# rpm

Compare installed RPM packages across remote systems via SSH.

## Requirements

| Requirement                                            | Description               |
| ------------------------------------------------------ | ------------------------- |
| [Python](https://www.python.org/) 3.14+                | Interpreter               |
| [tabulate](https://github.com/astanin/python-tabulate) | Pretty-print tabular data |
| [yaspin](https://github.com/pavdmyt/yaspin)            | Terminal spinner          |

Hosts must be reachable with passwordless (key-based) SSH; the script connects
with `BatchMode=yes` and will not prompt for a password.

## Usage

    python compare.py HOST HOST [HOST ...]

## Environment

| Variable                      | Default | Description                         |
| ----------------------------- | ------- | ----------------------------------- |
| `RPM_COMPARE_CONNECT_TIMEOUT` | `10`    | SSH connect timeout in seconds      |
| `RPM_COMPARE_TIMEOUT`         | `30`    | Overall timeout per host in seconds |

## Examples

    python compare.py server1 server2
    python compare.py web01 web02 web03
    RPM_COMPARE_TIMEOUT=120 python compare.py big01 big02
