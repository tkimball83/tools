# video

Flag video files with a non-English default audio track or extra non-English languages.

Only files with a known video extension (`VIDEO_EXTENSIONS` in the script) are
scanned. `.ts` and `.mts` files are only scanned if they look like MPEG transport
streams, so TypeScript sources are ignored. Files are listed relative to the
directory argument they were found under.

Audio tracks without a language tag count as `und` (undetermined), which is
flagged as non-English, so untagged files show up in the results.

## Requirements

| Requirement                                            | Description               |
| ------------------------------------------------------ | ------------------------- |
| [Python](https://www.python.org/) 3.14+                | Interpreter               |
| [libmediainfo](https://mediaarea.net/MediaInfo)        | Media metadata library    |
| [pymediainfo](https://github.com/sbraz/pymediainfo)    | MediaInfo wrapper         |
| [tabulate](https://github.com/astanin/python-tabulate) | Pretty-print tabular data |
| [yaspin](https://github.com/pavdmyt/yaspin)            | Terminal spinner          |

## Usage

    python check_language.py DIR [DIR ...]

## Environment

| Variable                 | Default | Description                       |
| ------------------------ | ------- | --------------------------------- |
| `CHECK_LANGUAGE_WORKERS` | `8`     | Number of concurrent scan threads |

## Examples

    python check_language.py /media/movies
    python check_language.py /media/movies /media/tv
    CHECK_LANGUAGE_WORKERS=4 python check_language.py /media/movies
