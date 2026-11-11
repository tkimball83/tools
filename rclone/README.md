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

| Flag | Default                                    | Description                         |
| ---- | ------------------------------------------ | ----------------------------------- |
| `-2` | `false`                                    | Enable HTTP/2 for rclone            |
| `-b` | `rclone`                                   | Change path to rclone binary        |
| `-c` | `$RCLONE_CONFIG`, else first found[^1]     | Change path to rclone config        |
| `-d` | `false`                                    | Enable rclone dry run               |
| `-f` | `tree.yaml` next to the script             | Change path to script config YAML   |
| `-h` |                                            | Show help and exit                  |
| `-k` | `32`                                       | Change rclone checkers setting      |
| `-l` | `0`                                        | Change rclone bandwidth limit       |
| `-q` | `false`                                    | Disable rclone progress             |
| `-s` | `shyaml`                                   | Change path to shyaml binary        |
| `-t` | `16`                                       | Change rclone transfers setting     |
| `-v` | `false`                                    | Enable rclone verbose (`-vv` debug) |

[^1]: `~/.config/rclone/rclone.conf`, then `/etc/rclone/rclone.conf`.

Progress is only shown when stdout is a terminal. All options are checked with
`rclone version` before the first sync, so a bad value fails fast.

## Configuration

Syncs are defined in `tree.yaml`:

```yaml
tree:
  - trunk: primary          # source remote
    branches:
      - name: backup-a      # no leafs: sync the whole remote
      - name: backup-b
        leafs:              # only sync these paths
          - Documents
          - Pictures
```

Each branch is synced from its trunk with `rclone sync`, which **deletes files on
the branch that are not on the trunk**. Use `-d` to preview changes first.

The whole file is checked before anything is synced. Any problem stops the run
with nothing synced:

- an unknown key, such as a misspelled `leafs` (which would otherwise sync the
  whole remote)
- a `trunk` or `name` that is not a remote in the rclone config
- a leaf that is not a plain relative path: no leading `/`, and no `.`, `..`
  or empty parts anywhere in it (names like `.hidden` are fine)
- a `branches` or `leafs` that is not a non-empty list
- a branch that names its own trunk
- syncs that write to overlapping paths (such as `backup:` and
  `backup:Documents`), or a remote that is both synced to and synced from,
  because `rclone sync` deletes anything not in the source

Every `trunk`, `name` and leaf must be a YAML string, so quote values that YAML
would read as another type, such as `"1.10"`, `"yes"`, `"2024"` or
`"2024-01-01"`. Exact duplicate syncs run once.

## Examples

    ./tree.sh -d
    ./tree.sh -q
    ./tree.sh -v
    ./tree.sh -vv
    ./tree.sh -2
    ./tree.sh -k 16
    ./tree.sh -l 100M
    ./tree.sh -b ~/.local/bin/rclone -c ~/.config/rclone/rclone.conf
    ./tree.sh -s /opt/homebrew/bin/shyaml
