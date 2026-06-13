"""Codex-backed evaluation agent."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..specification import ApiConfig, EvalResult, JudgeVerdict, ProverRunResult, RubricItemVerdict, TaskSpec
from .prompts import EVAL_SYSTEM_PROMPT, eval_user_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_CONFIG = Path(__file__).with_name("eval.yaml")


@dataclass
class CodexEvalConfig:
    api: ApiConfig
    home_dir: Path
    timeout_seconds: int = 10000
    model_reasoning_effort: str = "xhigh"
    binary: str = "codex"

    @classmethod
    def load(cls, path: str | Path = DEFAULT_EVAL_CONFIG) -> "CodexEvalConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        api = ApiConfig(**data["api"])
        codex = data.get("codex") or {}
        home_dir = Path(codex.get("home_dir", "breakthrough_eval/eval/codex_home"))
        if not home_dir.is_absolute():
            home_dir = REPO_ROOT / home_dir
        return cls(
            api=api,
            home_dir=home_dir,
            timeout_seconds=int(codex.get("timeout_seconds", 10000)),
            model_reasoning_effort=str(codex.get("model_reasoning_effort", "xhigh")),
            binary=str(codex.get("binary", "codex")),
        )


def render_config_toml(cfg: CodexEvalConfig, workdir: Path) -> str:
    provider = cfg.api.model_provider
    return (
        f'model_provider = "{provider}"\n'
        f'model = "{cfg.api.model}"\n'
        f'review_model = "{cfg.api.model}"\n'
        f'model_reasoning_effort = "{cfg.model_reasoning_effort}"\n'
        "disable_response_storage = true\n"
        'network_access = "enabled"\n'
        'approvals_reviewer = "user"\n'
        "\n"
        f"[model_providers.{provider}]\n"
        f'name = "{provider}"\n'
        f'base_url = "{cfg.api.base_url.rstrip("/")}"\n'
        f'wire_api = "{cfg.api.wire_api}"\n'
        "requires_openai_auth = true\n"
        "\n"
        "[features]\n"
        "goals = true\n"
        "\n"
        f'[projects."{REPO_ROOT}"]\n'
        'trust_level = "trusted"\n'
        "\n"
        f'[projects."{workdir}"]\n'
        'trust_level = "trusted"\n'
    )


def render_auth_json(cfg: CodexEvalConfig) -> str:
    return json.dumps({"OPENAI_API_KEY": cfg.api.api_key}, ensure_ascii=False, indent=2) + "\n"


class CodexEvalAgent:
    name = "codex-eval"

    def __init__(self, config: CodexEvalConfig):
        self.config = config

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": "CodexEvalAgent",
            "model": self.config.api.model,
            "base_url": self.config.api.base_url,
            "home_dir": str(self.config.home_dir),
            "timeout_seconds": self.config.timeout_seconds,
        }

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, task: TaskSpec, prover: ProverRunResult, job_dir: Path) -> EvalResult:
        job_dir = job_dir.resolve()
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        workspace_dir = output_dir / "workspace"
        input_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._write_inputs(task, prover, input_dir)
        self._run_codex(task, job_dir, output_dir, workspace_dir)
        return self._read_result(task, prover, output_dir / "result.json")

    def _run_codex(self, task: TaskSpec, job_dir: Path, output_dir: Path, workspace_dir: Path) -> None:
        if not self.available():
            raise RuntimeError(f"codex CLI 不可用: {self.config.binary}")
        self.config.home_dir.mkdir(parents=True, exist_ok=True)
        (self.config.home_dir / "config.toml").write_text(
            render_config_toml(self.config, job_dir),
            encoding="utf-8",
        )
        (self.config.home_dir / "auth.json").write_text(
            render_auth_json(self.config),
            encoding="utf-8",
        )
        last_message_path = workspace_dir / "last_message.txt"
        prompt = EVAL_SYSTEM_PROMPT + "\n\n" + eval_user_prompt(task)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.config.home_dir)
        cmd = [
            self.config.binary,
            "exec",
            "--sandbox", "danger-full-access",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "-c", 'approval_policy="never"',
            "-c", "web_search=disabled",
            "-o", str(last_message_path),
            "--cd", str(job_dir),
            "-",
        ]
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=self.config.timeout_seconds,
        )
        _write_trace(output_dir / "trace.jsonl", proc.stdout, proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"codex eval 退出码 {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
        if not (output_dir / "result.json").exists():
            raise RuntimeError("codex eval 没有写 output/result.json")

    def _write_inputs(self, task: TaskSpec, prover: ProverRunResult, input_dir: Path) -> None:
        (input_dir / "task.md").write_text(_task_md(task), encoding="utf-8")
        (input_dir / "prover.md").write_text(_numbered(prover.proof_text), encoding="utf-8")
        (input_dir / "golden.md").write_text(_golden_md(task), encoding="utf-8")
        (input_dir / "rubric.json").write_text(
            json.dumps([r.model_dump(mode="json") for r in task.rubric], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (input_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "job_id": prover.job_id,
                    "task_id": task.task_id,
                    "model": prover.model,
                    "harness": prover.harness,
                    "hint_level": prover.hint_level,
                    "trial": prover.trial,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _read_result(self, task: TaskSpec, prover: ProverRunResult, path: Path) -> EvalResult:
        data = json.loads(path.read_text(encoding="utf-8"))
        verdicts = [
            RubricItemVerdict(
                item_id=str(item["item_id"]),
                passed=bool(item.get("passed", False)),
                justification=str(item.get("justification", "")),
                cited_lines=[int(x) for x in item.get("cited_lines", [])],
                confidence=float(item.get("confidence", 0.5)),
            )
            for item in data.get("item_verdicts", [])
        ]
        judge = JudgeVerdict(
            judge_name=self.name,
            item_verdicts=verdicts,
            overall_valid=bool(data.get("overall_valid", False)),
            alternative_valid=bool(data.get("alternative_valid", False)),
            notes=str(data.get("notes", "")),
        )
        result = build_eval_result(
            task=task,
            prover=prover,
            judge=judge,
            needs_human_review=bool(data.get("needs_human_review", False)),
        )
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


def _write_trace(path: Path, stdout: str, stderr: str) -> None:
    content = stdout
    if stderr.strip():
        content += json.dumps(
            {"type": "process.stderr", "text": stderr},
            ensure_ascii=False,
        ) + "\n"
    path.write_text(content, encoding="utf-8")


def build_eval_result(
    task: TaskSpec,
    prover: ProverRunResult,
    judge: JudgeVerdict,
    needs_human_review: bool = False,
) -> EvalResult:
    consensus = {item.id: False for item in task.rubric}
    for verdict in judge.item_verdicts:
        if verdict.item_id in consensus:
            consensus[verdict.item_id] = verdict.passed
    revealed: set[str] = set()
    for hint in task.hint_ladder:
        if hint.level <= prover.hint_level:
            revealed.update(hint.reveals_rubric_items)
    item_ids = [item.id for item in task.rubric]
    earned_ids = [item_id for item_id in item_ids if item_id not in revealed]
    passed_items = sum(1 for ok in consensus.values() if ok)
    earned_passed = sum(1 for item_id in earned_ids if consensus.get(item_id))
    return EvalResult(
        job_id=prover.job_id,
        task_id=task.task_id,
        judges=[judge],
        item_consensus=consensus,
        passed_items=passed_items,
        total_items=len(item_ids),
        overall_valid=judge.overall_valid,
        alternative_valid=judge.alternative_valid,
        agreement=1.0,
        needs_human_review=needs_human_review,
        excluded=False,
        revealed_items=sorted(revealed),
        earned_passed_items=earned_passed,
        earned_total_items=len(earned_ids),
    )


def errored_eval_result(task: TaskSpec, prover: ProverRunResult, error: str) -> EvalResult:
    return EvalResult(
        job_id=prover.job_id,
        task_id=task.task_id,
        judges=[
            JudgeVerdict(
                judge_name=CodexEvalAgent.name,
                item_verdicts=[],
                overall_valid=False,
                notes=error,
                parse_failed=True,
            )
        ],
        total_items=len(task.rubric),
        needs_human_review=True,
        errored=True,
    )


def _task_md(task: TaskSpec) -> str:
    return "\n\n".join(
        [
            f"# {task.title}",
            f"- task_id: {task.task_id}\n- domain: {task.domain}\n- breakthrough_date: {task.breakthrough_date}\n- retrieval_cutoff: {task.retrieval_cutoff}",
            "## Problem\n" + task.problem_statement,
            "## Framing Notes\n" + task.problem_framing_notes,
        ]
    )


def _golden_md(task: TaskSpec) -> str:
    companions = "\n".join(f"- {item}" for item in task.golden_proof.companions) or "- none"
    return "\n\n".join(
        [
            "# Golden Reference",
            "## Primary Source\n" + task.golden_proof.primary,
            "## Companion Sources\n" + companions,
            (
                "## Fetch Instruction\n"
                "Fetch the primary source directly before judging. If it is an arXiv "
                "identifier, inspect the arXiv abstract page and PDF. Use companion "
                "sources when they help interpret the proof. Save any downloaded files "
                "or notes under output/workspace/."
            ),
            "## Local Summary\n" + task.golden_proof.proof_text,
        ]
    )


def _numbered(text: str) -> str:
    return "\n".join(f"{idx}: {line}" for idx, line in enumerate(text.splitlines(), 1))
