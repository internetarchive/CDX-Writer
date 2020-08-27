#!/usr/bin/env python
"""
Test cdx_writer.py with real-world W/ARCs.
"""
from __future__ import print_function
import pytest
import py
import sys
import os
import re
import commands
import subprocess
from pipes import quote
from hashlib import md5

data_dir = os.path.join(os.path.dirname(__file__), 'large_warcs')
warc_dir = data_dir

warcs = [
    dict(fn='YTV-20120204025848-crawl442/YTV-20120204035110-15431.warc.gz',
         file_md5='f06e02b7b777143c0eb67d9de45da8f4',
         cdx_md5='7a891b642febb891a6cf78511dc80a55'
         ),
    dict(fn='WIDE-20120121162724-crawl411/WIDE-20120121174231-03025.warc.gz',
         file_md5='f89b9b1b5f36d9c3039e2da2169e01d4'
         ),
    dict(fn='live-20120312105341306-00165-20120312171822397/live-20120312161414739-00234.arc.gz',
         file_md5='f6583963381dcc26c58a76bc433f2713'
         ),
    # missing filedesc:// header
    dict(fn='wb_urls.ia11013.20050517055850-c/wb_urls.ia11013.20050805040525.arc.gz',
         file_md5='8712de66615e4da87dfb524a5015e19f'
         ),
    dict(fn='ftp-mayn-de-2016-08-04/ftp.mayn.de.warc.gz',
        file_md5='19699891d68508d2a1105a5a2f8f35e6'
        )
    ]

testdir = py.path.local(__file__).dirpath()
cdx_writer = str(testdir / '../cdx_writer.py')

if sys.platform == 'darwin':
    TIMECMD = '/usr/bin/time -p '
else:
    TIMECMD = '/usr/bin/time --format=%e '

def file_md5(fn):
    hasher = md5()
    with open(str(fn), 'rb') as f:
        while True:
            data = f.read(8192)
            if not data: break
            hasher.update(data)
    return hasher.hexdigest()

@pytest.mark.parametrize("data", warcs)
def test_large_warcs(data, tmpdir):
    warc_fn = data['fn']
    warc_file = os.path.join(warc_dir, warc_fn)
    if not os.path.isfile(warc_file):
        pytest.skip("requires {} to run this test".format(warc_file))

    expected_cdx_md5 = data.get('cdx_md5')

    tmpcdx = tmpdir / 'tmp.cdx'

    cmd = TIMECMD + '%s %s >%s' % (
        cdx_writer, warc_fn, tmpcdx)
    print("  running", cmd)
    with py.path.local(warc_dir).as_cwd():
        status, output = commands.getstatusoutput(cmd)
    assert 0 == status
    print('time: ', output)
    print('size: ', os.path.getsize(warc_file))

    # translate CDX output into expected data format
    tmphashcdx = tmpdir / 'tmp.hashcdx'
    hashcdx(str(tmpcdx), str(tmphashcdx))

    # run diff to compare with expected
    exp = os.path.join(data_dir, re.sub(r'\.w?arc\.gz$', '.exp', warc_fn))
    if os.path.exists(exp):
        cmd = 'diff -u %r %r' % (exp, str(tmphashcdx))
        print("  running", cmd)
        status, output = commands.getstatusoutput(cmd)
        print(output)
        assert 0 == status

    if expected_cdx_md5:
        cdx_md5 = file_md5(tmpcdx)
        assert expected_cdx_md5 == cdx_md5

def run_cdx_writer(warc_file, output, basedir=None):
    if basedir:
        basedir = py.path.local(basedir)
        warc_file = basedir.bestrelpath(py.path.local(warc_file))
    else:
        basedir = py.path.local('.')
    cmd = [cdx_writer, warc_file]
    with basedir.as_cwd():
        p = subprocess.Popen(cmd, stdout=output)
    return p

def hashcdx(cdx, hashcdx):
    with open(cdx, 'rb') as f, open(hashcdx, 'wb') as w:
        for l in f:
            if not l.startswith(' '):
                urlkey, ts, original, rest = l.split(' ', 3)
                urlkey = md5(urlkey).hexdigest()
                original = md5(original).hexdigest()
                l = ' '.join([urlkey, ts, original, rest])
            w.write(l)

# main code for preparing test data
if __name__ == "__main__":
    import argparse

    def generate_expected(args):
        for w in warcs:
            warc_file = os.path.join(args.warcdir, w['fn'])
            out_file = os.path.join(args.datadir,
                                    re.sub(r'.w?arc\.gz$', '.cdx', w['fn']))
            assert out_file != warc_file
            outdir = os.path.dirname(out_file)
            if not os.path.isdir(outdir):
                os.makedirs(outdir)
            print("- Reading {}".format(warc_file), file=sys.stderr)
            print("  Writing {}".format(out_file), file=sys.stderr)
            with open(out_file, 'wb') as outf:
                p = run_cdx_writer(warc_file, outf, args.warcdir)
                rc = p.wait()
                assert rc == 0

    def generate_hashed(args):
        for w in warcs:
            in_file = os.path.join(args.datadir,
                                   re.sub(r'.w?arc\.gz$', '.cdx', w['fn']))
            out_file = os.path.join(args.datadir,
                                    re.sub(r'.w?arc\.gz$', '.exp', w['fn']))
            print("- Reading {}".format(in_file), file=sys.stderr)
            print("  Writing {}".format(out_file), file=sys.stderr)
            hashcdx(in_file, out_file)

    def download_warcs(args):
        fcount = 0
        for w in warcs:
            warc_file = os.path.join(args.warcdir, w['fn'])
            dldir = os.path.dirname(warc_file)
            if not os.path.isdir(dldir):
                os.makedirs(dldir)
            cmd = ("curl -b %s -L -o %s -w '%%{http_code}'"
                   " https://archive.org/download/%s" % (
                    args.cookie, warc_file, w['fn']))
            print("Running %s" % (cmd,), file=sys.stderr)
            status, output = commands.getstatusoutput(cmd)
            assert status == 0, "Download failed with status=%d" % (status,)

            sys.stderr.write("Checking checksum...")
            m = md5()
            with open(warc_file, 'rb') as f:
                while True:
                    d = f.read(32*1024)
                    if not d: break
                    m.update(d)
            digest = m.hexdigest()
            assert digest == w['file_md5'], (
                "MD5 sum does not match: expected %s, got %s" % (
                    w['file_md5'], digest)
                )
            sys.stderr.write("OK\n")
            fcount += 1
        print("Downloaded %d files in %s" % (fcount, args.warcdir), file=sys.stderr)

    parser = argparse.ArgumentParser()
    parser.add_argument("-w", "--warcdir", default=warc_dir)
    parser.add_argument("-d", "--datadir", default=data_dir)

    subparsers = parser.add_subparsers()

    parser_dl = subparsers.add_parser(
        "download", help="download test WARCs from petabox")
    parser_dl.add_argument(
        "-b", "--cookie",  help="authentication cookie file",
        default="~/.iaauth")
    parser_dl.set_defaults(func=download_warcs)

    parser_cdx = subparsers.add_parser(
        "cdx", help="generate CDX output with current cdx_writer.py")
    parser_cdx.set_defaults(func=generate_expected)

    parser_exp = subparsers.add_parser(
        "exp", help="generate expected data from CDX")
    parser_exp.set_defaults(func=generate_hashed)

    args = parser.parse_args()
    args.func(args)
