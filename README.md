# cdx_writer.py
Python script to create CDX index files of WARC data.

[![Build Status](https://travis-ci.org/internetarchive/CDX-Writer.png?branch=master)](https://travis-ci.org/internetarchive/CDX-Writer)

## Usage
Usage: `cdx_writer.py [options] warc.gz`

Options:

    -h, --help                  show this help message and exit
    --format=FORMAT             A space-separated list of fields [default: 'N b a m s k r M S V g']
    --use-full-path             Use the full path of the warc file in the 'g' field
    --file-prefix=FILE_PREFIX   Path prefix for warc file name in the 'g' field.
                                Useful if you are going to relocate the warc.gz file
                                after processing it.
    --all-records               By default we only index http responses. Use this flag
                                to index all WARC records in the file
    --screenshot-mode           Special Wayback Machine mode for handling WARCs
                                containing screenshots
    --exclude-list=EXCLUDE_LIST File containing url prefixes to exclude
    --stats-file=STATS_FILE     Output json file containing statistics


Output is written to stdout. The first line of output is the CDX header.
This header line begins with a space so that the cdx file can be passed
through `sort` while keeping the header at the top.

## Format
The supported format options are:

    M meta tags (AIF) *
    N massaged url
    S compressed record size
    V compressed arc file offset *
    a original url **
    b date **
    g file name
    k new style checksum *
    m mime type of original document *
    r redirect *
    s response code *

    * in alexa-made dat file
    ** in alexa-made dat file meta-data line

More information about the CDX format syntax can be found here:
http://www.archive.org/web/researcher/cdx_legend.php

## Installation

Unfortunately, this script is not propery packaged and cannot be installed via
pip. `pip install -r requirements.txt` will install dependencies. Then
./cdx_writer.py should run.

## Differences between cdx_writer.py and access-access cdx files
The CDX files produced by the [archive-access](http://sourceforge.net/projects/archive-access/)
package produce different CDX lines in these cases:

### Differences in SURTs:
* archive-access doesn't encode the %7F character in SURTs

### Differences in MIME Type:
* archive-access does not parse mime type for large warc payloads, and just returns 'unk'
* If the HTTP Content-Type header is sent with a blank value, archive-access
returns the value of the previous header as the mime type. cdx_writer.py
returns 'unk' in this case. Example WARC Record (returns "close" as the mime type):
    <code>...Content-Length: 0\r\nConnection: close\r\nContent-Type: \r\n\r\n\r\n\r\n</code>

### Differences in Redirect urls:
* archive-access does not escape whitespace, cdx_writer.py uses %20 escaping so we can split these files on whitespace.
* archive-access removes unicode characters from redirect urls, cdx_writer.py version keeps them
* archive-access does not decode html entities in redirect urls
* archive-access sometimes does not turn relative URLs into absolute urls
* archive-access sometimes does not remove /../ from redirect urls
* archive-access uses the value from the previous HTTP header for the redirect url if the location header is empty
* cdx_writer.py only looks for http-equiv=refresh meta tag inside head elements

### Differences in Meta Tags:
* cdx_writer.py only looks for meta tags in the head element
* archive-access version doesn't parse multiple html meta tags, only the first one
* archive-access misses FI meta tags sometimes
* cdx_writer.py always returns tags in A, F, I order. archive-access does not use a consistent order


### Differences in HTTP Response Codes
* archive-access returns response code 0 if HTTP header line contains unicode:
    <code>HTTP/1.1 302 D\xe9plac\xe9 Temporairement\r\n...</code>
