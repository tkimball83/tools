# tools

Assorted utility scripts and tools. The Python scripts need Python 3.14 or newer.

## Available tools

| Script                    | Description                                               |
| ------------------------- | --------------------------------------------------------- |
| `rclone/tree.sh`          | Sync between multiple rclone remotes via YAML config      |
| `rpm/compare.py`          | Compare installed rpm packages across systems via ssh     |
| `video/check_language.py` | Flag videos with a non-English default or extra languages |

## Development

    make          # create venv, install dependencies and the pre-commit hook
    make lint     # run pre-commit on all files
    make check    # run self-tests

The venv is built with `python3.14`; use `make PYTHON=/path/to/python3` for
another Python 3.14+ interpreter, after `make clean` if a venv already exists.

## License

[MIT](LICENSE)
