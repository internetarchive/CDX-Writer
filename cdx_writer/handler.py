import os
import re
import sys
import io
import base64
import hashlib
import urlparse
from datetime import datetime
from httplib import IncompleteRead
import six

from six.moves.urllib.parse import urljoin

class FieldValueError(Exception):
    pass

class RecordStreamReader(io.RawIOBase):
    def __init__(self, stream):
        """This class serves two purposes:

        1. shields underlining `stream` (RecordStream) from cascading close() calls.
        2. makes `stream` io.IOBase compatible. in particular, defines `readable`,
           `readinto`, and `readlines` (CDX-Writer does not use it, but `HTTPResponse`
           assumes defined)

        (probably record._content_file should be wrapped by something like this
        at warctools level)
        """
        self.stream = stream

    # this is the same as the default implementation, but this is the key.
    # def close(self):
    #     pass

    def readable(self):
        return True

    def readinto(self, b):
        return self.stream.readinto(b)

class DigestingReader(io.RawIOBase):
    def __init__(self, stream):
        """DigstingReader transparently reads from `stream`, computing
        digest hash as data go through it.
        `complete` flag is set when it sees EOF.
        """
        self.stream = stream
        self.digester = hashlib.sha1()
        self.complete = False

    def b32digest(self):
        # returns bytes
        return base64.b32encode(self.digester.digest())

    def readable(self):
        return True

    def readinto(self, b):
        n = self.stream.readinto(b)
        if n > 0:
            self.digester.update(b[:n])
        else:
            self.complete = True
        return n

from six.moves.http_client import (
    HTTPResponse, BadStatusLine, UnknownProtocol,
    CONTINUE,NO_CONTENT, NOT_MODIFIED
)
from six.moves.http_client import HTTPMessage as HTTPMessageBase

if issubclass(HTTPResponse, object):
    HTTPResponseParserBase = HTTPResponse
else:
    # Python 2 HTTPResponse is an old class. We need a new-style base class
    HTTPResponseParserBase = type('HTTPResponseParserBase', (object, HTTPResponse), {})

# headers not saved in HTTPResponseParser - those not useful for indexing
# archive records. set-cookie is the most common offender.
HEADERS_IGNORED = set([
    'set-cookie',
    'x-powered-by',
    'cache-control',
    'x-ua-compatible',
    'connection',
    'x-content-type=options',
    'p3p',
    'x-xss-protection',
    'x-frame-options',
    'x-download-options',
    'expires',
    'pragma'
])

class HTTPMessage(HTTPMessageBase):
    # Overridden to disable check on the number of header fields. There are
    # too many cases of more than 30K header fields in real world. In order
    # to reduce memory consumption, we discard those headers not interesting
    # to archive record indexing.
    def readheaders(self):
        self.dict = {}
        self.unixfrom = ''
        self.headers = hlist = []
        self.status = ''
        headerseen = ""
        firstline = 1
        startofline = unread = tell = None
        if hasattr(self.fp, 'unread'):
            unread = self.fp.unread
        elif self.seekable:
            tell = self.fp.tell
        while True:
            if tell:
                try:
                    startofline = tell()
                except IOError:
                    startofline = tell = None
                    self.seekable = 0
            line = self.fp.readline()
            if not line:
                self.status = 'EOF in headers'
                break
            # Skip unix From name time lines
            if firstline and line.startswith('From '):
                self.unixfrom = self.unixfrom + line
                continue
            firstline = 0
            if headerseen and line[0] in ' \t':
                # XXX Not sure if continuation lines are handled properly
                # for http and/or for repeating headers
                # It's a continuation line.
                if headerseen.lower() not in HEADERS_IGNORED:
                    hlist.append(line)
                    self.addcontinue(headerseen, line.strip())
                continue
            elif self.iscomment(line):
                # It's a comment.  Ignore it.
                continue
            elif self.islast(line):
                # Note! No pushback here!  The delimiter line gets eaten.
                break
            headerseen = self.isheader(line)
            if headerseen:
                # It's a legal header line, save it.
                if headerseen.lower() not in HEADERS_IGNORED:
                    hlist.append(line)
                    self.addheader(headerseen, line[len(headerseen)+1:].strip())
                continue
            elif headerseen is not None:
                # An empty header name. These aren't allowed in HTTP, but it's
                # probably a benign mistake. Don't add the header, just keep
                # going.
                continue
            else:
                # It's not a header line; throw it back and stop here.
                if not self.dict:
                    self.status = 'No headers'
                else:
                    self.status = 'Non-header line where header expected'
                # Try to undo the read.
                if unread:
                    unread(line)
                elif tell:
                    self.fp.seek(startofline)
                else:
                    self.status = self.status + '; bad seek'
                break

class HTTPResponseParser(HTTPResponseParserBase):
    def __init__(self, fileobj):
        if not hasattr(fileobj, 'peek'):
            fileobj = io.BufferedReader(fileobj)
        self.fileobj = fileobj
        HTTPResponse.__init__(self, self, strict=0, buffering=False)
        self.begin()

    def makefile(self, a, b=None):
        """makes HTTPResponseParser look like a socket to HTTPResponse."""
        return self.fileobj

    def _read_status(self):
        """Overridden to handle truncated response and old-style revisits."""
        try:
            return super(HTTPResponseParser, self)._read_status()
        except BadStatusLine as ex:
            # no response from server, even a status line. old-style revisit
            # record looks like this.
            return 'HTTP/0.9', 200, ""

    # overridden to make it mmore lenient with sloppy implementations
    def begin(self):
        if self.msg is not None:
            return
        # this loop is skipping 100 response and its headers.
        # I've never seen 100 responses captured in response records,
        # but there may be in the future.
        while True:
            version, status, reason = self._read_status()
            if status != CONTINUE:
                break
            while True:
                skip = self.fp.readline()
                if not skip.strip():
                    break
        self.status = status
        self.reason = reason.strip()
        if version == 'HTTP/1.0':
            self.version = 10
        elif version.startswith('HTTP/1.'):
            self.version = 11 # HTTP/1.x (x>=1) is assumed to be HTTP/1.1
        elif version == 'HTTP/0.9':
            self.version = 9
        elif version.startswith('HTTP/'):
            # added branch - assume any other 'HTTP/...' as HTTP/1.0
            self.version = 10
        else:
            raise UnknownProtocol(version)

        self.will_close = 1

        if self.version == 9:
            self.length = None
            self.chunked = 0
            self.msg = HTTPMessage(io.BytesIO())
            return

        self.msg = HTTPMessage(self.fp, 0)
        self.msg.fp = None

        tr_enc = self.msg.getheader('transfer-encoding')
        if tr_enc and tr_enc.lower() == 'chunked':
            self.chunked = 1
            self.chunk_left = None
        else:
            self.chunked = 0

        # it should not be critical to have length. we could
        # blanketly set length = None?
        length = self.msg.getheader('content-length')
        if length and not self.chunked:
            try:
                self.length = int(length)
            except ValueError:
                self.length = None
            else:
                if self.length < 0:
                    self.length = None
        else:
            self.length = None

        if (status == NO_CONTENT or status == NOT_MODIFIED or
            100 <- status < 200):
            self.length = 0

    # io.RawIOBase compatibility (necessary for handling calls from DigestingReader)
    def readinto(self, b):
        # HTTPResponse does not implement readinto. implement
        # with read()
        size = len(b)
        if size == 0:
            return 0
        # silently catches IncompleteRead due to truncated chunk encoding,
        # and returns whatever available so far.
        try:
            d = self.read(size)
        except IncompleteRead as ex:
            d = ex.partial
        if not d:
            return 0
        b[:len(d)] = d
        return len(d)

    # chunked attribute is replaced with chunked property so that HTTPResponseParserr
    # can parse with "Transfer-Encoding:chunked" and non-chunked-encoded content.
    # we know there are captures with one space after hex chunk size. allowing multiple of
    # space and TABs just in case.
    RE_CHUNK_HEADER = re.compile(br'[0-9a-f]+[ \t]*\r\n', re.I)
    def _get_chunked(self): # getter
        return self._chunked
    def _set_chunked(self, chunked):
        # assumes self.fileobj is positioned at the start of response body when
        # HTTPResponse assigns 1 to self.chunked
        if chunked == 1:
            probably_chunk_header = self.fileobj.peek(16)
            if not self.RE_CHUNK_HEADER.match(probably_chunk_header):
                # does not look like chunked-encoded. clear chunked flag.
                chunked = 0
                # debugging aid
                self.transfer_encoding_ignored_because = probably_chunk_header[:16]
        self._chunked = chunked
    chunked = property(_get_chunked, _set_chunked)

class RecordContent(object):
    def __init__(self, stream):
        """Object for accessing archive record content ("block" or "payload).
        Provides content SHA1.

        :param stream: file-like object for reading raw block content. must be positioned
            at the start of block content (i.e. just after record header)
        """
        assert stream is not None
        self._block_reader = stream
        self.content_reader = self._setup_content_reader()

    def _setup_content_reader(self):
        return DigestingReader(self._block_reader)

    def content_digest(self):
        if not self.content_reader.complete:
            while True:
                d = self.content_reader.read(4096)
                if not d:
                    break
        assert self.content_reader.complete
        return self.content_reader.b32digest()

class HttpResponseRecordContent(RecordContent):
    """:class:`RecordContent` for accessing HTTP response content.
    `content_reader` and `content_digest` works for HTTP response content, not record block.
    """
    def _setup_content_reader(self):
        self._http_response = HTTPResponseParser(self._block_reader)
        return DigestingReader(self._http_response)

    def response_code(self):
        return self._http_response.status

    def get_http_header(self, name):
        return self._http_response.getheader(name)

    def content_type(self):
        ct = self.get_http_header('content-type')
        return ct

    def http_version(self):
        # int: 9, 10, or 11
        return self._http_response.version


def to_unicode(s, charset):
    if isinstance(s, str):
        if charset is None:
            #try utf-8 and hope for the best
            s = s.decode('utf-8', 'replace')
        else:
            try:
                s = s.decode(charset, 'replace')
            except LookupError:
                s = s.decode('utf-8', 'replace')
    return s

# these function used to be used for normalizing URL for ``redirect`` field.
def urljoin_and_normalize(base, url, charset):
    """urlparse.urljoin removes blank fragments (trailing #),
    even if allow_fragments is set to True, so do this manually.

    Also, normalize /../ and /./ in url paths.

    Finally, encode spaces in the url with %20 so that we can
    later split on whitespace.

    Usage (run doctests with  `python -m doctest -v cdx_writer.py`):
    >>> base = 'http://archive.org/a/b/'
    >>> url  = '/c/d/../e/foo'
    >>> print CDX_Writer.urljoin_and_normalize(base, url, 'utf-8')
    http://archive.org/c/e/foo

    urljoin() doesn't normalize if the url starts with a slash, and
    os.path.normalize() has many issues, so normalize using regexes

    >>> url = '/foo/./bar/#'
    >>> print CDX_Writer.urljoin_and_normalize(base, url, 'utf-8')
    http://archive.org/foo/bar/#

    >>> base = 'http://archive.org'
    >>> url = '../site'
    >>> print CDX_Writer.urljoin_and_normalize(base, url, 'utf-8')
    http://archive.org/site

    >>> base = 'http://www.seomoz.org/page-strength/http://www.example.com/'
    >>> url  = 'http://www.seomoz.org/trifecta/fetch/page/http://www.example.com/'
    >>> print CDX_Writer.urljoin_and_normalize(base, url, 'utf-8')
    http://www.seomoz.org/trifecta/fetch/page/http://www.example.com/
    """

    url  = to_unicode(url, charset)

    #the base url is from the arc/warc header, which doesn't specify a charset
    base = to_unicode(base, 'utf-8')

    try:
        joined_url = urlparse.urljoin(base, url)
    except ValueError:
        #some urls we find in arc files no longer parse with python 2.7,
        #e.g. 'http://\x93\xe0\x90E\x83f\x81[\x83^\x93\xfc\x97\xcd.com/'
        return '-'

    # We were using os.path.normpath, but had to add too many patches
    # when it was doing the wrong thing, such as turning http:// into http:/
    m = re.match('(https?://.+?/)', joined_url)
    if m:
        domain = joined_url[:m.end(1)]
        path   = joined_url[m.end(1):]
        if path.startswith('../'):
            path = path[3:]
        norm_url = domain + re.sub('/[^/]+/\.\./', '/', path)
        norm_url = re.sub('/\./', '/', norm_url)
    else:
        norm_url = joined_url

    # deal with empty query strings and empty fragments, which
    # urljoin sometimes removes
    if url.endswith('?') and not norm_url.endswith('?'):
        norm_url += '?'
    elif url.endswith('#') and not norm_url.endswith('#'):
        norm_url += '#'

    #encode spaces
    return norm_url.replace(' ', '%20')


class NoCloseBufferedReader(io.BufferedReader):
    """BufferedReader that does not propagate close to underlining stream.
    """
    def close(self):
        pass

class RecordHandler(object):
    _content = None
    _content_factory = RecordContent

    def __init__(self, record, env):
        """Defines default behavior for all fields.
        Field values are defined as properties with name
        matching descriptive name in ``field_map``.
        """
        self.record = record
        self.env = env
        self.urlkey = env.urlkey

    def get_record_header(self, name):
        return self.record.get_header(name)

    @property
    def content(self):
        if self._content is None:
            reader = RecordStreamReader(self.record.content_file)
            self._content = self._content_factory(reader)
        return self._content

    @property
    def massaged_url(self):
        """massaged url / field "N".
        """
        url = self.safe_url().encode('latin1')
        try:
            return self.urlkey(url)
        except:
            return self.original_url

    @property
    def date(self):
        """date / field "b".
        """
        # warcs and arcs use a different date format
        # consider using dateutil.parser instead
        record = self.record
        if record.date is None:
            # TODO: in strict mode, this shall be a fatal error.
            return None
        elif record.date.isdigit():
            date_len = len(record.date)
            if 14 == date_len:
                #arc record already has date in the format we need
                return record.date
            elif 14 < date_len and date_len <= 18:
                #some arc records have 15-digit dates: 201512000000000
                #some arc records have 16-digit dates: 2000082305410049
                #some arc records have 18-digit dates: 200009180023002953
                return record.date[:14]
            elif 12 == date_len:
                #some arc records have 12-digit dates: 200011201434
                return record.date + '00'
            elif 10 == date_len:
                #some arc records have 10-digit dates: 2016020900
                return record.date + '0000'
        elif re.match('[a-f0-9]+$', record.date):
            #some arc records have a hex string in the date field
            return None
        elif re.match('[0-9]{14,18}[a-zA-Z]+$', record.date):
            #some arc records are like this: 20160211000000jpg
            return record.date[:14]

        #warc record
        try:
            date = datetime.strptime(record.date[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError as ex:
            raise FieldValueError('Archive-Date: {}'.format(record.date))
        return date.strftime("%Y%m%d%H%M%S")

    def safe_url(self):
        url = self.record.url
        # There are few arc files from 2002 that have non-ascii characters in
        # the url field. These are not utf-8 characters, and the charset of the
        # page might not be specified, so use chardet to try and make these usable.
        if isinstance(url, bytes):
            url = url.decode('latin1')

        # due to a descrepancy in WARC 1.0 specification, certain versions of
        # wget put < > around WARC-Target-URI value.
        if url[:1] == '<' and url[-1:] == '>':
            url = url[1:-1]

        # Some arc headers contain urls with the '\r' character, which will cause
        # problems downstream when trying to process this url, Browsers typially
        # remove '\r'. So do we.
        url = url.replace('\r', '')
        # %-encode other white spaces that can cuase CDX parsing problems
        def percent_hex(m):
            return "%{:02X}".format(ord(m.group(0)))
        url = re.sub(r'[ \r\n\x0c\x08]', percent_hex, url)

        return url

    @property
    def original_url(self):
        """original url / field "a".
        """
        url = self.safe_url()
        return url.encode('latin1')

    def _normalize_content_type(self, content_type):
        if content_type is None:
            return 'unk'

        # if multiple header fields with the same name occur, HTTPResponse
        # joins values with comma, in reverse order of occurrence (this is prescribed by
        # RFC 2616)
        content_type = content_type.split(',', 1)[0]

        # some http responses end abruptly: ...Content-Length: 0\r\nConnection: close\r\nContent-Type: \r\n\r\n\r\n\r\n'
        content_type = content_type.strip()
        if content_type == '':
            return 'unk'
        # Alexa arc files use 'no-type' instead of 'unk'
        if content_type == 'no-type':
            return 'unk'

        mtype, d, params = content_type.partition(';')
        mtype = mtype.strip().lower()

        if re.match(r'[-a-z0-9.+/]+$', mtype):
            return mtype
        else:
            return 'unk'

    @property
    def mime_type(self):
        """mime type / field "m".
        """
        return 'warc/' + self.record.type

    @property
    def response_code(self):
        """response code / field "s".
        """
        return None

    @property
    def new_style_checksum(self):
        """new style checksum / field "k".
        """
        return self.content.content_digest()

    @property
    def redirect(self):
        """redirect / field "r".
        """
        # only meaningful for HTTP response records.
        return None

    @property
    def compressed_record_size(self):
        """compressed record size / field "S".
        """
        size = self.record.compressed_record_size
        if size is None:
            return None
        return str(size)

    @property
    def compressed_arc_file_offset(self):
        """compressed arc file offset / field "V".
        """
        return str(self.record.offset)

    @property
    def aif_meta_tags(self):
        """AIF meta tags / field "M".
        robot metatags, if present, should be in this order:
        A, F, I. Called "robotsflags" in Wayback.
        """
        return None

    @property
    def file_name(self):
        """file name / field "g".
        """
        return self.env.warc_path

class WarcinfoHandler(RecordHandler):
    """``wercinfo`` record handler."""
    #similar to what what the wayback uses:
    fake_build_version = "archive-commons.0.0.1-SNAPSHOT-20120112102659-python"

    @property
    def massaged_url(self):
        return self.original_url

    @property
    def original_url(self):
        return 'warcinfo:/%s/%s' % (
            self.env.in_file, self.fake_build_version
        )

    @property
    def mime_type(self):
        return 'warc-info'

class HttpHandler(RecordHandler):
    """Logic common to all HTTP response records
    (``response`` and ``revisit`` record types).
    """
    _content_factory = HttpResponseRecordContent
    meta_tags = None

    @property
    def redirect(self):
        # write "S" for self-redirects (i.e. it's a redirect ot the  same
        # key.
        location = self.content.get_http_header('location')
        if location:
            try:
                location = urljoin(self.record.url, location)
                key = self.urlkey(location)
            except Exception:
                # ignore invalid URL
                key = None
            if key == self.massaged_url:
                return 'S'

        # Aaron, Ilya, and Kenji have proposed using '-' in the redirect column
        # unconditionally, after a discussion on Sept 5, 2012. It turns out the
        # redirect column of the cdx has no effect on the Wayback Machine, and
        # there were issues with parsing unescaped characters found in redirects.
        return None

        # followig code is copied from old version before refactoring. it will
        # not work with new structure.

        # response_code = self.response_code
        #
        # ## It turns out that the refresh tag is being used in both 2xx and 3xx
        # ## responses, so always check both the http location header and the meta
        # ## tags. Also, the java version passes spaces through to the cdx file,
        # ## which might break tools that split cdx lines on whitespace.
        #
        # #only deal with 2xx and 3xx responses:
        # #if 3 != len(response_code):
        # #    return '-'
        #
        # charset = self.parse_charset()
        #
        # #if response_code.startswith('3'):
        # location = self.parse_http_header('location')
        # if location:
        #     return self.urljoin_and_normalize(record.url, location, charset)
        # #elif response_code.startswith('2'):
        # if self.meta_tags and 'refresh' in self.meta_tags:
        #     redir_loc = self.meta_tags['refresh']
        #     m = re.search('\d+\s*;\s*url=(.+)', redir_loc, re.I) #url might be capitalized
        #     if m:
        #         return self.urljoin_and_normalize(record.url, m.group(1), charset)
        #
        # return '-'

class ResponseHandler(HttpHandler):
    """Handler for HTTP response with archived content (``response`` record type).
    """

    def __init__(self, record, env):
        super(ResponseHandler, self).__init__(record, env)
        self.meta_tags = self.parse_meta_tags()

    def is_response(self):
        return self.record.is_response()

    @property
    def mime_type(self):
        if self.is_response():
            content_type = self.content.content_type()
        else:
            # For ARC record content_type returns response content type from
            # ARC header line.
            # XXX - this is often wrong. We ought to look at HTTP response
            # header if ARC header value is suspicious.
            content_type = self.record.content_type
        return self._normalize_content_type(content_type)

    RE_RESPONSE_LINE = re.compile(
        r'HTTP(?P<version>/\d\.\d)? (?P<statuscode>\d+)')

    @property
    def response_code(self):
        code = self.content.response_code()
        return code and format(code)

    @property
    def new_style_checksum(self):
        return self.content.content_digest()

    def parse_meta_tags(self):
        """We want to parse meta tags in <head>, even if not direct children.
        e.g. <head><noscript><meta .../></noscript></head>

        What should we do about multiple meta tags with the same name?
        currently, we append the content attribs together with a comma seperator.

        We use either the 'name' or 'http-equiv' attrib as the meta_tag dict key.
        """

        if self.mime_type != 'text/html':
            return None

        if self.content is None:
            return None

        meta_tags = {}

        #lxml.html can't parse blank documents
        # reading max 5MB into memory
        html_str = self.content.content_reader.read(5 * 1024 * 1024)
        if '' == html_str:
            return meta_tags

        #lxml can't handle large documents
        if self.record.content_length > self.env.lxml_parse_limit:
            return meta_tags

        # lxml was working great with ubuntu 10.04 / python 2.6
        # On ubuntu 11.10 / python 2.7, lxml exhausts memory hits the ulimit
        # on the same warc files. Unfortunately, we don't ship a virtualenv,
        # so we're going to give up on lxml and use regexes to parse html :(

        for x in re.finditer("(<meta[^>]+?>|</head>)", html_str, re.I):
            #we only want to look for meta tags that occur before the </head> tag
            if x.group(1).lower() == '</head>':
                break
            name = None
            content = None

            m = re.search(r'''\b(?:name|http-equiv)\s*=\s*(['"]?)(.*?)(\1)[\s/>]''', x.group(1), re.I)
            if m:
                name = m.group(2).lower()
            else:
                continue

            m = re.search(r'''\bcontent\s*=\s*(['"]?)(.*?)(\1)[\s/>]''', x.group(1), re.I)
            if m:
                content = m.group(2)
            else:
                continue

            if name not in meta_tags:
                meta_tags[name] = content
            else:
                if 'refresh' != name:
                    #for redirect urls, we only want the first refresh tag
                    meta_tags[name] += ',' + content

        return meta_tags

    @property
    def aif_meta_tags(self):
        x_robots_tag = self.content.get_http_header('x-robots-tag')

        robot_tags = []
        if self.meta_tags and 'robots' in self.meta_tags:
            robot_tags += self.meta_tags['robots'].split(',')
        if x_robots_tag:
            robot_tags += x_robots_tag.split(',')
        robot_tags = [x.strip().lower() for x in robot_tags]

        s = ''
        if 'noarchive' in robot_tags:
            s += 'A'
        if 'nofollow' in robot_tags:
            s += 'F'
        if 'noindex' in robot_tags:
            s += 'I'

        # IA-proprietary extension 'P' flag for password protected pages.
        # crawler adds special header to WARC record, whose value consists
        # of three values separated by comma. The first value is a number
        # of attempted logins (so >0 value means captured with login).
        # Example: ``1,1,http://(com,example,)/``
        sfps = self.get_record_header('WARC-Simple-Form-Province-Status')
        if sfps:
            sfps = sfps.split(',', 2)
            try:
                if int(sfps[0]) > 0:
                    s += 'P'
            except ValueError as ex:
                pass

        return ''.join(s) if s else None

class ResourceHandler(RecordHandler):
    """HTTP resource record (``resource`` record type).
    """
    @property
    def mime_type(self):
        return self._normalize_content_type(self.record.content_type)

class RevisitHandler(HttpHandler):
    """HTTP revisit record (``revisit`` record type).

    Note that this handler does not override ``mime_type``.
    Hence ``mime_type`` field will always be ``warc/revisit``.
    """
    @property
    def new_style_checksum(self):
        digest = self.get_record_header('WARC-Payload-Digest')
        if digest is None:
            return None
        return digest.replace('sha1:', '')

class FtpHandler(RecordHandler):
    @property
    def mime_type(self):
        return self._normalize_content_type(self.record.content_type)

    @property
    def response_code(self):
        """Always return 226 assuming all ftp captures are successful ones.
        Code 226 represents successful completion of file action, and it is
        what org.apache.commons.net.ftp.FTPClient#getReplyCode() (used by
        Heritrix) returns upon successful download.

        Ref. https://en.wikipedia.org/wiki/List_of_FTP_server_return_codes
        """
        return '226'

    @property
    def new_style_checksum(self):
        # For "resource" record, block is also a payload. So
        # Both WARC-Payload-Digest and WARC-Block-Digest is valid.
        # wget uses Block. Heritirx uses Payload.
        digest = self.get_record_header('WARC-Payload-Digest')
        if digest:
            return digest.replace('sha1:', '')
        digest = self.get_record_header('WARC-Block-Digest')
        if digest:
            return digest.replace('sha1:', '')

        return self.content.content_digest()

class VideoMetaHandler(RecordHandler):
    @property
    def original_url(self):
        return 'http://wayback-metadata.archive.org/video-meta/' + self.safe_url()

    @property
    def massaged_url(self):
        return 'org,archive,wayback-metadata)/video-meta/' + self.urlkey(self.safe_url())

    @property
    def mime_type(self):
        return self._normalize_content_type('application/json;generator-youtube-dl')
