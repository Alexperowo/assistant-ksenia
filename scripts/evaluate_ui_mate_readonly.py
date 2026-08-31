from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from butler.config import load_settings  # noqa: E402
from butler.model_manager import ResidentModelPool  # noqa: E402
from butler.ui_deliberation import UIDeliberator  # noqa: E402
from butler.ui_evaluation import (  # noqa: E402
    UIEvaluationError,
    evaluate_ui_proposal,
    load_ui_manifest,
)
from butler.ui_mate import UIMateProposer  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only UI-Mate corpus gate. Reads screenshots, proposes actions and never "
            "calls Windows input APIs."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--review",
        action="store_true",
        help="Add the configured independent policy review and at most one revision.",
    )
    parser.add_argument(
        "--start-models",
        action="store_true",
        help="Start configured resident models and stop only those started by this process.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    settings = load_settings(PROJECT_ROOT)
    try:
        cases = load_ui_manifest(args.manifest)
    except UIEvaluationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    pool = ResidentModelPool(settings)
    active_before = set(pool.running_states())
    started_here: list[str] = []
    results: list[dict[str, object]] = []
    started = time.monotonic()
    try:
        if args.start_models:
            states = pool.start_all()
            started_here = [role for role in states if role not in active_before]
        proposer = UIMateProposer(settings)
        deliberator = UIDeliberator(settings) if args.review else None
        for case in cases:
            case_started = time.monotonic()
            try:
                screenshot = case.screenshot.read_bytes()
                if deliberator is None:
                    proposal = proposer.propose(
                        case.task, screenshot, mode_name="fast"
                    )
                    review_payload = None
                    revisions = 0
                    review_approved = True
                else:
                    deliberation = deliberator.deliberate(
                        case.task,
                        lambda task, feedback: proposer.propose(
                            task,
                            screenshot,
                            mode_name=deliberator.profile.proposal_mode,
                            feedback=feedback,
                        ),
                    )
                    proposal = deliberation.proposal
                    review_payload = {
                        "decision": deliberation.review.decision,
                        "reason": deliberation.review.reason,
                    }
                    revisions = deliberation.revision_count
                    review_approved = deliberation.approved
                evaluation = evaluate_ui_proposal(case, proposal)
                reasons = list(evaluation.reasons)
                if not review_approved:
                    reasons.append("independent policy review не одобрил действие")
                results.append(
                    {
                        "id": case.case_id,
                        "passed": evaluation.passed and review_approved,
                        "action": evaluation.action,
                        "reasons": reasons,
                        "proposal": proposal.as_untrusted_payload(),
                        "review": review_payload,
                        "revision_count": revisions,
                        "duration_ms": round(
                            (time.monotonic() - case_started) * 1000
                        ),
                        "executed": False,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "id": case.case_id,
                        "passed": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "duration_ms": round(
                            (time.monotonic() - case_started) * 1000
                        ),
                        "executed": False,
                    }
                )
    finally:
        for role in reversed(started_here):
            pool.manager(role).stop()

    passed = sum(1 for result in results if result.get("passed") is True)
    payload = {
        "ok": passed == len(cases),
        "case_count": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "review_enabled": bool(args.review),
        "executed_actions": 0,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
