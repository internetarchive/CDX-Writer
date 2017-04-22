#!/usr/bin/env python
# -*- coding: utf-8 -*-
import pytest
import py
import sys
from cdx_writer import timestamp_is_valid, http_date_timestamp


@pytest.mark.parametrize("ts,result", [
    ("20121506143600", False),
    ("20121206143600", True),
    ("20010161126200", False),
    ("00001012262000", False),
    ("30001012262000", False),
    ("20011012262000", True),
    ])
def test_timestamp_is_valid(ts, result):
    assert timestamp_is_valid(ts) == result


@pytest.mark.parametrize("http_date,ts", [
    ("Fri, 16 Aug 2013 10:51:08 GMT", "20130816105108"),
    ("Friday invalid", None),
    ("16 Aug 2013 10:51", "20130816105100"),
    ("16 08 2013 10:51", "20130816105100"),
    (None, None)
    ])
def test_http_date_timestamp(http_date, ts):
    assert http_date_timestamp(http_date) == ts


def test_invalid_warc_date(tmpdir):
    """invalid_range_digit_date.arc.gz has invalid WARC timestamp 20001812054100
    and HTTP Date: Wed, 23 Aug 2000 05:42:20 GMT. Output timestamp should be:
    20000823054220
    """
    testdir = py.path.local(__file__).dirpath()
    datadir = testdir / "small_warcs"
    sys.path[0:0] = (str(testdir / '..'),)
    cdx_writer = __import__('cdx_writer')

    args = ["--all-records", "invalid_range_digit_date.arc.gz"]
    with datadir.as_cwd():
        outpath = tmpdir / 'stdout'
        saved_stdout = sys.stdout
        sys.stdout = outpath.open(mode='wb')
        try:
            cdx_writer.main(args)
        finally:
            sys.stdout.close()
            output = outpath.read_binary()
            sys.stdout = saved_stdout
    ts = output.splitlines()[2].split(" ")[1]
    assert ts == "20000823054220"
