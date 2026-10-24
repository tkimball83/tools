# rclone

A YAML-controlled bash script to sync between multiple rclone remotes.

## Requirements

| Requirement                                | Description               |
| ------------------------------------------ | ------------------------- |
| [rclone](https://github.com/rclone/rclone) | rsync for cloud storage   |
| [shyaml](https://github.com/0k/shyaml)     | YAML for the command line |

## Usage

    ./tree.sh [options]

## Options

| Flag | Default                   | Description                         |
| ---- | ------------------------- | ----------------------------------- |
| `-2` | `false`                   | Enable HTTP/2 for rclone            |
| `-b` | `rclone`                  | Change path to rclone binary        |
| `-c` | `null`                    | Change path to rclone config        |
| `-d` | `false`                   | Enable rclone dry run               |
| `-f` | `tree.yaml`               | Change path to script config YAML   |
| `-h` |                           | Show help and exit                  |
| `-k` | `32`                      | Change rclone checkers setting      |
| `-l` | `0`                       | Change rclone bandwidth limit       |
| `-q` | `false`                   | Disable rclone verbose              |
| `-s` | `shyaml`                  | Change path to shyaml binary        |
| `-t` | `16`                      | Change rclone transfers setting     |
| `-v` | `-v`                      | Set rclone verbose (`-vv` debug)    |

If `-c` is not provided, the script uses the first existing config at:
`~/.config/rclone/rclone.conf`, then `/etc/rclone/rclone.conf`.

## Examples

    ./tree.sh -d
    ./tree.sh -q
    ./tree.sh -dvv
    ./tree.sh -2
    ./tree.sh -k 16
    ./tree.sh -l 100M
    ./tree.sh -b ~/.local/bin/rclone -c ~/.config/rclone/rclone.conf
    ./tree.sh -s /opt/homebrew/bin/shyaml
