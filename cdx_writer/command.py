from __future__ import print_function
import sys
import os
import json
from operator import attrgetter
from optparse import OptionParser
import traceback
import zlib

from hanzo.warctools import ArchiveRecord
from surt import surt

from .dispatcher import DefaultDispatcher, AllDispatcher
from .screenshot import ScreenshotDispatcher
from .video      import VideoDispatcher
from .exclusion import PrefixExclusion
from .handler import RecordHandler
from .archive import ArchiveRecordReader

class CDX_Writer(object):
    _mode_dispatcher = {
        'default': DefaultDispatcher(),
        'all': AllDispatcher(),

        'screenshot': ScreenshotDispatcher(),
    }

    def __init__(self, in_file, out_file=sys.stdout, format="N b a m s k r M S V g",
                 warc_path=None, dispatch_mode=None,
                 exclude_list=None, canonicalizer=None,
                 error_handler=None):
        """This class is instantiated for each web archive file and generates
        CDX from it.

        :param in_file: input web archive file name
        :param out_file: file object to write CDX to
        :param format: CDX field specification string.
        :param warc_path: filename field value (literal or callable taking `file` as an argument)
        :param dispatch_mode: string representing dispatchers to use
        :param exclude_list: a file containing a list of excluded URLs
        :param canonicalizer_options: URL canonicalizer options
        """
        self.field_map = {'M': 'AIF meta tags',
                          'N': 'massaged url',
                          'S': 'compressed record size',
                          'V': 'compressed arc file offset',
                          'a': 'original url',
                          'b': 'date',
                          'g': 'file name',
                          'k': 'new style checksum',
                          'm': 'mime type',
                          'r': 'redirect',
                          's': 'response code',
                         }

        self.in_file = in_file
        self.out_file = out_file
        self.format = format

        self.fieldgetter = self._build_fieldgetter(self.format.split())

        self.dispatcher = self._mode_dispatcher[dispatch_mode or 'default']
        # self.dispatcher = RecordDispatcher(
        #     all_records=all_records, screenshot_mode=screenshot_mode)

        self.urlkey = canonicalizer or surt

        #Large html files cause lxml to segfault
        #problematic file was 154MB, we'll stop at 5MB
        self.lxml_parse_limit = 5 * 1024 * 1024

        if callable(warc_path):
            self.warc_path = warc_path(in_file)
        else:
            self.warc_path = warc_path or in_file

        self.exclusion = exclude_list or False

        self.error_handler = error_handler or IgnoreCommonErrorHandler()

    def _build_fieldgetter(self, fieldcodes):
        """Return a callable that collects CDX field values from a
        :class:`RecordHandler` object, according to CDX field specification
        `fieldcodes`.

        :param fieldcodes: a list of single-letter CDX field codes.
        """
        attrs = []
        for field in fieldcodes:
            if field not in self.field_map:
                raise ParseError('unknown field; {}'.format(field))
            attrs.append(self.field_map[field].replace(' ', '_').lower())
        return attrgetter(*attrs)

    def urlkey(self):
        # replaced in __init__
        pass

    def should_exclude(self, urlkey):
        return self.exclusion and self.exclusion.excluded(urlkey)


    def make_cdx(self):
        self.stats = {
            'num_records_processed': 0,
            'num_records_included': 0,
            'num_records_filtered': 0,
            'num_records_failed': 0
        }
        try:
            if hasattr(self.out_file, "write"):
                self._make_cdx(self.out_file, self.stats)
            else:
                with open(self.out_file, "wb") as w:
                    self._make_cdx(w, self.stats)
        except Exception as ex:
            if not self.error_handler.should_continue(ex):
                raise

    def _make_cdx(self, out_file, stats):
        out_file.write(b' CDX ' + self.format + b'\n') #print header

        record_reader = ArchiveRecordReader(self.in_file)
        while True:
            offset = record_reader._stream_offset()
            try:
                record = next(record_reader, None)
                if record is None:
                    break
                stats['num_records_processed'] += 1
                handler = self.dispatcher.dispatch(record, self)
                if not handler:
                    # ensure record is read off in this cycle, so that errors,
                    # if any, are reported against this record. If we don't do
                    # this here, decompression error will happen during the
                    # next next(record_reader) and thus be associated with a
                    # wrong record. We need to do this for all cases that goes
                    # without writing out a cdx line.
                    # XXX this is oascure. should have a distinct method for
                    # doing this.
                    _ = record.compressed_record_size
                    continue
                assert isinstance(handler, RecordHandler)

                ### arc files from the live web proxy can have a negative content length and a missing payload
                ### check the content_length from the arc header, not the computed payload size returned by record.content_length
                # XXX move this to dispatcher.
                content_length_str = record.get_header(record.CONTENT_LENGTH)
                if content_length_str:
                    try:
                        clen = int(content_length_str)
                        if clen < 0:
                            # see above
                            _ = record.compressed_record_size
                            continue
                    except ValueError:
                        pass

                surt = handler.massaged_url
                if self.should_exclude(surt):
                    stats['num_records_filtered'] += 1
                    # see above
                    _ = record.compressed_record_size
                    continue

                ### precalculated data that is used multiple times
                # self.headers, self.content = self.parse_headers_and_content(record)
                # self.mime_type             = self.get_mime_type(record, use_precalculated_value=False)

                values = [b'-' if v is None else v for v in self.fieldgetter(handler)]
                out_file.write(b' '.join(values) + b'\n')
                #record.dump()
                stats['num_records_included'] += 1
            except Exception as ex:
                stats['num_records_failed'] += 1
                if self.error_handler.should_continue(ex, offset):
                    if is_decompression_error(ex):
                        # in case of a decompression error, decompressor may
                        # have read far beyond the next helthy record. go back
                        # to the beginning of the record and search for the
                        # next possible record.
                        record_reader.reset(offset)
                    else:
                        record_reader.reset()
                else:
                    print('!!! error while processing a record at %d' % offset,
                          file=sys.stderr)
                    raise

        record_reader.close()

def is_decompression_error(error):
    if isinstance(error, zlib.error):
        # error at zlib level
        return True
    if isinstance(error, IOError):
        # error at gzip level; CRC/length check failure
        msg = str(error)
        if msg == "Incorrect length of data produced":
            return True
        if msg.startswith("CRC check failed "):
            return True
        if msg == 'Not a gzipped file':
            return True
    return False

class ErrorHandler(object):
    def __init__(self):
        self.errors_seen = set()

    def report_ignored_error(self, error, offset=None):
        # do not print stack trace for the second and later occurrence of
        # error from the same source location.
        etype, value, tb = sys.exc_info()
        bottom_tb = traceback.extract_tb(tb, 1)
        # use (file path, line number) as key
        key = tuple(bottom_tb[:2])
        seen_before = (key in self.errors_seen)
        self.errors_seen.add(key)

        if offset is None:
            print('!!! ignoring an error [[', file=sys.stderr)
        else:
            print('!!! ignoring an error while processing a record at %d [['
                  % offset, file=sys.stderr)
        if seen_before:
            print(traceback.format_exception_only(etype, value)[-1],
                  end='', file=sys.stderr)
        else:
            traceback.print_exc(limit=3)
        print('!!! ]]', file=sys.stderr)

    def should_continue(self, error, offset=None):
        if self.should_ignore(error):
            self.report_ignored_error(error, offset)
            return True
        return False

class BlanketErrorHandler(ErrorHandler):
    def __init__(self, ignore):
        super(BlanketErrorHandler, self).__init__()
        self.ignore = ignore

    def should_ignore(self, error):
        return self.ignore

class IgnoreCommonErrorHandler(ErrorHandler):
    def should_ignore(self, error):
        msg = str(error)
        fqtype = '{0.__module__}.{0.__name__}'.format(type(error))
        if msg == 'Failed to guess compression':
            # w/arc does snot look like gzip at all
            return True
        if fqtype in ('httplib.LineTooLong', 'http.client.LineTooLong'):
            # can happen for otherwise normal HTTP response, but typically
            # non-HTTP response in "response" record.
            return True
        if is_decompression_error(error):
            return True
        if msg.startswith('Malformed ARC header:'):
            # ARc header is broken beyond we can (willing to) rescue
            return True
        if fqtype == ('cdx_writer.archive.RecordParseError'):
            return True
        if fqtype == ('cdx_writer.handler.FieldValueError'):
            return True
        return False

def error_handler_type(v):
    if v == 'none':
        return BlanketErrorHandler(False)
    if v == 'all':
        return BlanketErrorHandler(True)
    if v == 'common':
        return IgnoreCommonErrorHandler()
    raise ValueError('ignore_error must be one of {none,common,all}')

def main(args=None):

    parser = OptionParser(usage="%prog [options] warc.gz [output_file.cdx]")
    parser.set_defaults(format        = "N b a m s k r M S V g",
                        use_full_path = False,
                        file_prefix   = None,
                        all_records   = False,
                        screenshot_mode = False,
                        exclude_list    = None,
                        canonicalizer_options = []
                       )

    parser.add_option("--format",  dest="format", help="A space-separated list of fields [default: '%default']")
    parser.add_option("--use-full-path", dest="use_full_path", action="store_true", help="Use the full path of the warc file in the 'g' field")
    parser.add_option("--file-prefix",   dest="file_prefix", help="Path prefix for warc file name in the 'g' field."
                      " Useful if you are going to relocate the warc.gz file after processing it."
                     )
    parser.add_option("--all-records", dest="dispatch_mode", action="store_const", const="all",
                      help="By default we only index http responses. Use this flag to index all WARC records in the file")
    parser.add_option("--screenshot-mode", dest="dispatch_mode", action="store_const", const="screenshot",
                      help="Special Wayback Machine mode for handling WARCs containing screenshots")
    parser.add_option("--exclude-list", dest="exclude_list", help="File containing url prefixes to exclude")
    parser.add_option("--stats-file", dest="stats_file", help="Output json file containing statistics")
    parser.add_option("--no-host-massage", dest="canonicalizer_options",
                      action='append_const', const=('host_massage', False),
                      help='Turn off host_massage (ex. stripping "www.")')
    parser.add_option("--ignore-error", choices=['none', 'common', 'all'],
                      default='common')

    if args is None:
        args = sys.argv[1:]
    options, input_files = parser.parse_args(args=args)

    if len(input_files) != 2:
        if len(input_files) == 1:
            input_files.append(sys.stdout)
        else:
            parser.print_help()
            return -1

    if options.use_full_path:
        warc_path = lambda fn: os.path.abspath(fn)
    elif options.file_prefix:
        warc_path = lambda fn: os.path.join(options.file_prefix, fn)
    else:
        warc_path = None

    def canonicalizer(url, options=dict(options.canonicalizer_options)):
        return surt(url, **options)

    exclude_list = options.exclude_list and PrefixExclusion(
            options.exclude_list, canonicalizer)

    error_handler = error_handler_type(options.ignore_error)

    cdx_writer = CDX_Writer(input_files[0], input_files[1],
                            format=options.format,
                            warc_path=warc_path,
                            dispatch_mode=options.dispatch_mode,
                            exclude_list=exclude_list,
                            canonicalizer=canonicalizer,
                            error_handler=error_handler
                           )
    try:
        cdx_writer.make_cdx()
    finally:
        if options.stats_file is not None:
            with open(options.stats_file, 'w') as f:
                json.dump(cdx_writer.stats, f, indent=4)
    return 0
