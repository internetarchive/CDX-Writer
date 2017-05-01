#!/usr/bin/env python
# -*- coding: utf-8 -*-
import pytest
import py
import sys


testdir = py.path.local(__file__).dirpath()
datadir = testdir / "check_datetime_warcs"
sys.path[0:0] = (str(testdir / '..'),)
cdx_writer = __import__('cdx_writer')


@pytest.mark.parametrize("ts,exc", [
    ("20121506143600", cdx_writer.InvalidTsMonth),
    ("20010161126200", cdx_writer.InvalidTsDay),
    ("00001012262000", cdx_writer.InvalidTsYear),
    ("30001012262000", cdx_writer.InvalidTsYear),
    ])
def test_timestamp_is_valid_exception(ts, exc):
    with pytest.raises(exc):
        cdx_writer.timestamp_is_valid(ts)


@pytest.mark.parametrize("ts", [
    "20011012262000",
    "20121206143600"
    ])
def test_timestamp_is_valid(ts):
    assert cdx_writer.timestamp_is_valid(ts) is True


@pytest.mark.parametrize("http_date,ts", [
    ("Fri, 16 Aug 2013 10:51:08 GMT", "20130816105108"),
    ("Friday invalid", None),
    ("16 Aug 2013 10:51", "20130816105100"),
    ("16 08 2013 10:51", "20130816105100"),
    (None, None)
    ])
def test_http_date_timestamp(http_date, ts):
    assert cdx_writer.http_date_timestamp(http_date) == ts

@pytest.mark.parametrize(["warc", "ts", "repair_ts"], [
    # month invalid (18) so corrected from HTTP Date
    ("invalid_range_digit_date.arc.gz", "20001812054100", None),
    ("invalid_range_digit_date.arc.gz", "20000812054100", "--repair-ts"),
    # WARC date correct so --repair-ts returns exactly the same result
    ("uncompressed_correct_warc_date.warc", "20110307082935", None),
    ("uncompressed_correct_warc_date.warc", "20110307082935", "--repair-ts"),
    # invalid WARC date & HTTP date (`Date: Wed, 23 \x Aug 2000 05:42:20 GMT`)
    # so we return "-" in both cases
    ("rec_corrupt.warc.gz", "-", None),
    ("rec_corrupt.warc.gz", "-", "--repair-ts"),
    # invalid WARC day, corrected via HTTP Date
    ("rec2_date_format.warc.gz", "20080462204825", None),
    ("rec2_date_format.warc.gz", "20080402204825", "--repair-ts")
    ])
def test_invalid_warc_date(warc, ts, repair_ts, tmpdir):
    """--repair-ts uses HTTP Date month if available to generate timestamp.
    """
    if repair_ts:
        args = ["--all-records", repair_ts, warc]
    else:
        args = ["--all-records", warc]
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
    assert ts == output.splitlines()[-1].split(" ")[1]
