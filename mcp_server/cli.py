"""Try the career-evidence tools from Terminal, without Claude:

  .venv-mcp/bin/python -m mcp_server.cli list
  .venv-mcp/bin/python -m mcp_server.cli search "test lab scaling"
  .venv-mcp/bin/python -m mcp_server.cli metrics "Example Product"
  .venv-mcp/bin/python -m mcp_server.cli check "Scaled the test lab to 50+ devices"
"""
import json
import sys

from .server import check_claim, get_verified_metrics, list_documents, search_evidence

USAGE = __doc__


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "search", "metrics", "check"):
        print(USAGE)
        sys.exit(1)
    cmd, arg = sys.argv[1], " ".join(sys.argv[2:]) or None
    if cmd == "list":
        result = list_documents()
    elif cmd == "search":
        result = search_evidence(arg or "")
    elif cmd == "metrics":
        result = get_verified_metrics(arg)
    else:
        result = check_claim(arg or "")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
