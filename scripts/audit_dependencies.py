"""Query OSV for the locked registry dependencies; sends only names/versions."""

import json
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def main():
    lock = tomllib.loads(Path("uv.lock").read_text())
    packages = [p for p in lock["package"] if "registry" in p.get("source", {})]
    payload = {
        "queries": [
            {
                "package": {"name": p["name"], "ecosystem": "PyPI"},
                "version": p["version"],
            }
            for p in packages
        ]
    }
    request = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise RuntimeError("Dependency audit response exceeds size limit")
    results = json.loads(body)["results"]
    if len(results) != len(packages):
        raise RuntimeError("Incomplete dependency audit response")
    findings = [
        {
            "name": p["name"],
            "version": p["version"],
            "advisories": r.get("vulns", []),
            "next_page_token": r.get("next_page_token"),
        }
        for p, r in zip(packages, results, strict=True)
    ]
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "https://api.osv.dev/v1/querybatch",
        "registry_packages": findings,
        "unscanned_sources": [
            {"name": p["name"], "source": p["source"]}
            for p in lock["package"]
            if "registry" not in p.get("source", {})
        ],
        "limitation": "Advisory matching does not verify source code, deployed contracts, or absence of undisclosed vulnerabilities.",
    }
    Path("evidence/dependency-audit.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    affected = sum(bool(r["advisories"] or r["next_page_token"]) for r in findings)
    print(
        f"Checked {len(packages)} locked registry packages; {affected} require review. Git sources are listed separately."
    )
    return bool(affected)


if __name__ == "__main__":
    sys.exit(main())
