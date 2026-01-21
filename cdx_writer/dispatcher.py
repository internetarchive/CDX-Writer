from .handler import (RecordHandler, ResponseHandler, RevisitHandler,
                      ResourceHandler, FtpHandler, WarcinfoHandler, VideoMetaHandler)

__all__ = [
    'RecordDispatcher', 'DefaultDispatcher', 'AllDispatcher'
]

class RecordDispatcher(object):
    _cache = None
    def dispatch(self, record, env):
        record_type = record.type
        if self._cache is None:
            self._cache = {}
        if record_type in self._cache:
            disp = self._cache[record_type]
        else:
            attr = "dispatch_{}".format(record_type)
            disp = getattr(self, attr, None)
            if disp is None:
                disp = getattr(self, "dispatch_any", None)
            self._cache[record_type] = disp
        if disp:
            handler = disp(record, env)
            if isinstance(handler, type):
                handler = handler(record, env)
            return handler
        return None

class DefaultDispatcher(RecordDispatcher):
    def dispatch_response(self, record, env):
        # probbaly it's better to test for "dns:" scheme?
        if record.content_type in ('text/dns',):
            return None
        if record.ip_address == b'127.0.0.1':
            return None

        handler = ResponseHandler(record, env)

        # exclude 304 Not Modified responses - impossible to playback
        if handler.response_code == '304':
            return None
        # exclude ARC record for failed liveweb proxy - not a capture
        # they all have "0.0.0.0" as IP-address, but this alone is not safficient
        # as there are also valid captures with "0.0.0.0" IP-address.
        # The first line of content is either "HTTP 502 Bad Gateway" or
        # "HTTP 504 Gateway Timeout". HTTPResponseParser does not recognize this
        # as valid HTTP status line and assumes HTTP/0.9 (first line is treated as
        # response content.). It'll be more robust to peak at the first line.
        ipaddr = handler.record.get_header('IP-address')
        content_type = handler.record.content_type
        if (ipaddr == b"0.0.0.0" and content_type == b'unk' and
            handler.content.http_version() == 9):
            return None

        return handler

    def dispatch_revisit(self, record, env):
        # exclude 304 Not Modified revisits (unless --all-records)
        if record.get_header('WARC-Profile') and record.get_header(
                'WARC-Profile').endswith('/revisit/server-not-modified'):
            return None
        if record.ip_address == b'127.0.0.1':
            return None
        return RevisitHandler

    def dispatch_resource(self, record, env):
        # wget saves resource records with wget agument and logging
        # output at the end of the WARC. those need to be skipped.
        if record.url.startswith('ftp://'):
            return FtpHandler
        elif record.url.startswith(('http://', 'https://')):
            return ResourceHandler
        return None
    
    def dispatch_metadata(self, record, env):
        content_type = record.content_type

        if content_type and content_type.startswith('application/json;generator-youtube-dl'):
            return VideoMetaHandler
        return None

class AllDispatcher(DefaultDispatcher):

    def dispatch_response(self, record, env):
        return ResponseHandler

    def dispatch_revisit(self, record, env):
        return RevisitHandler

    def dispatch_resource(self, record, env):
        disp = super(AllDispatcher, self).dispatch_resource(record, env)
        return disp or RecordHandler

    def dispatch_warcinfo(self, record, env):
        return WarcinfoHandler

    def dispatch_any(self, record, env):
        return RecordHandler
