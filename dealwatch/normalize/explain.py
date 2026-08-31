"""CLI: trace one title through the normalization pipeline.

    python -m dealwatch.normalize.explain --profile profiles/thinkpad-t14.yaml \
        --title "Lenovo ThinkPad T14 Gen 1 16GB 256GB" \
        [--subtitle ...] [--condition-id 3000]

Prints every pipeline stage's outcome (design.md §5), then the final
SpecResult. Useful for answering "why did this title end up in bucket X"
or "why didn't this reject rule fire" without re-running the full report.
"""

import argparse

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import normalize_verbose


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--title", required=True)
    parser.add_argument("--subtitle", default=None)
    parser.add_argument("--condition-id", type=int, default=None)
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    listing_fields = {
        "title": args.title,
        "subtitle": args.subtitle,
        "condition_id": args.condition_id,
    }

    result, trace = normalize_verbose(profile, listing_fields)

    print(f"title: {args.title!r}")
    for line in trace:
        print(f"  {line}")
    print(f"spec_status: {result.spec_status}")
    print(f"reject_rule_id: {result.reject_rule_id}")
    print(f"bucket_key: {result.bucket_key}")
    print(f"spec: {result.spec}")


if __name__ == "__main__":
    main()
