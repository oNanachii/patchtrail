"""Preview/commit an operator-owned local source manifest without executing it."""
import argparse
import json
from pathlib import Path

from .config import Settings
from .projects import ProjectCatalog, load_manifest, preview_project
from .workspace import WorkspaceError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--commit", action="store_true", help="Commit the reviewed allowlist; default is preview only")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        preview = preview_project(args.source, manifest)
        if args.commit:
            # Settings reads existing environment only; never load/print .env or keys.
            directory = (args.data_dir if args.data_dir is not None else Settings().data_dir) / "projects"
            result = ProjectCatalog(directory).import_project(args.source, manifest,
                                                                expected_inventory=preview["files"])
            result["execution_performed"] = False
        else:
            result = preview
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkspaceError, OSError, ValueError) as exc:
        # Paths can contain private operator information: no traceback or absolute path dump.
        print(json.dumps({"error": {"code": "import_rejected", "message": str(exc)[:300]}}
                         if isinstance(exc, WorkspaceError) else
                         {"error": {"code": "import_rejected", "message": "The source or manifest could not be read"}},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
