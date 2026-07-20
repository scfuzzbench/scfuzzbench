import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def job_blocks(contents: str) -> dict[str, str]:
    _, marker, jobs = contents.partition("\njobs:\n")
    if not marker:
        raise AssertionError("workflow has no jobs mapping")
    matches = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\n", jobs))
    return {
        match.group(1): jobs[
            match.start() : (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(jobs)
            )
        ]
        for index, match in enumerate(matches)
    }


def run_bodies(contents: str) -> list[str]:
    lines = contents.splitlines()
    bodies: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        indentation = len(line) - len(line.lstrip())
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.strip():
                candidate_indent = len(candidate) - len(candidate.lstrip())
                if candidate_indent <= indentation:
                    break
            body.append(candidate)
        bodies.append("\n".join(body))
    return bodies


class WorkflowSafetyContractTests(unittest.TestCase):
    def test_queue_max_is_narrow_and_all_mutation_groups_are_lossless(self):
        expected = {
            ("benchmark-run.yml", "admission"),
            ("benchmark-run.yml", "benchmark-run"),
            ("benchmark-run.yml", "finalize-failed-run"),
            ("benchmark-cleanup.yml", "cleanup"),
            ("terraform-cd.yml", "terraform"),
        }
        actual: set[tuple[str, str]] = set()
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for job_name, block in job_blocks(path.read_text()).items():
                if "queue:" not in block:
                    continue
                actual.add((path.name, job_name))
                self.assertIn("queue: max", block)
                self.assertIn("cancel-in-progress: false", block)
        self.assertEqual(expected, actual)

        ci = workflow("ci.yml")
        exact_ignore = (
            "-ignore '^unexpected key \"queue\" for \"concurrency\" section\\."
            " expected one of \"cancel-in-progress\", \"group\"$'"
        )
        self.assertIn(exact_ignore, ci)
        self.assertEqual(1, ci.count("-ignore "))
        self.assertIn(
            "analysis.tests.test_workflow_safety_contracts", ci
        )

    def test_every_privileged_job_depends_on_main_authorization(self):
        privileged_jobs = {
            "benchmark-run.yml": {
                "prepare-run",
                "admission",
                "benchmark-run",
                "finalize-failed-run",
            },
            "benchmark-cleanup.yml": {"discover", "cleanup"},
            "terraform-cd.yml": {"terraform"},
            "benchmark-preliminary.yml": {
                "discover",
                "analyze",
                "refresh-docs",
            },
            "benchmark-release.yml": {
                "discover",
                "release",
                "refresh_docs",
            },
            "docs.yml": {"build", "deploy"},
            "s3-inspect.yml": {"inspect"},
        }
        for filename, names in privileged_jobs.items():
            contents = workflow(filename)
            blocks = job_blocks(contents)
            self.assertIn("authorize-main", blocks)
            self.assertIn('refs/heads/main', blocks["authorize-main"])
            for name in names:
                with self.subTest(workflow=filename, job=name):
                    self.assertIn("authorize-main", blocks[name])
                    self.assertRegex(blocks[name], r"(?m)^    needs:")

        self.assertIn(
            "group: pages-${{ github.ref }}", workflow("docs.yml")
        )
        self.assertIn(
            "group: benchmark-preliminary-results-${{ github.ref }}",
            workflow("benchmark-preliminary.yml"),
        )

    def test_failure_finalization_is_cancellation_aware_and_idempotent(self):
        contents = workflow("benchmark-run.yml")
        blocks = job_blocks(contents)
        benchmark = blocks["benchmark-run"]
        finalizer = blocks["finalize-failed-run"]

        self.assertIn("!cancelled()", benchmark)
        self.assertNotIn("always()", benchmark)
        self.assertEqual(1, contents.count("always()"))
        self.assertIn("always()", finalizer)
        self.assertIn("needs.admission.result != 'success'", finalizer)
        self.assertIn("needs.benchmark-run.result != 'success'", finalizer)
        self.assertIn("mark-failed-if-reserved", finalizer)
        self.assertNotIn("set +e", finalizer)

    def test_run_revalidates_reservation_after_lock_before_terraform(self):
        contents = workflow("benchmark-run.yml")
        block = job_blocks(contents)["benchmark-run"]
        self.assertIn("Revalidate reserved run after acquiring run lock", block)
        self.assertIn("--metadata-json", block)
        self.assertIn("--expected-status reserved", block)
        self.assertIn("--expected-commit", block)
        self.assertLess(
            block.index("Configure AWS credentials"),
            block.index("Revalidate reserved run after acquiring run lock"),
        )
        self.assertLess(
            block.index("Revalidate reserved run after acquiring run lock"),
            block.index("Terraform init (remote backend)"),
        )

    def test_captured_terraform_state_lists_disable_color_and_wrapper(self):
        capture_pattern = re.compile(
            r"(?m)^\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)="
            r"\$\((?P<command>terraform[^\n]*\bstate\s+list\b[^\n]*)\)\s*$"
        )
        captures: dict[tuple[str, str], str] = {}
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for match in capture_pattern.finditer(path.read_text()):
                captures[(path.name, match.group("variable"))] = match.group(
                    "command"
                )

        self.assertEqual(
            {
                ("benchmark-cleanup.yml", "state_resources"),
                ("benchmark-run.yml", "existing_state"),
                ("terraform-cd.yml", "state_resources"),
            },
            set(captures),
        )
        for location, command in captures.items():
            with self.subTest(location=location):
                self.assertRegex(
                    command,
                    r"\bstate\s+list\s+-no-color(?:\s|$)",
                )
                filename, variable = location
                capturing_job = next(
                    block
                    for block in job_blocks(workflow(filename)).values()
                    if f"{variable}=$(" in block
                )
                self.assertRegex(
                    capturing_job,
                    r"(?ms)uses: hashicorp/setup-terraform@v3\n"
                    r"\s+with:\n"
                    r"(?:\s+[^\n]+\n)*?"
                    r"\s+terraform_wrapper: false$",
                )

    def test_fresh_state_check_rejects_wrapper_output_but_direct_cli_is_empty(self):
        body = next(
            body
            for body in run_bodies(workflow("benchmark-run.yml"))
            if "existing_state=$(terraform " in body
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            direct_bin = temp / "direct-bin"
            direct_bin.mkdir()
            terraform = direct_bin / "terraform"
            terraform.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_TERRAFORM_ERROR:-0}" == "1" ]]; then
  echo "backend unavailable" >&2
  exit 1
fi
if [[ " $* " == *" state list -no-color "* ]]; then
  exit 0
fi
printf '\\033[0m'
"""
            )
            terraform.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{direct_bin}:{os.environ['PATH']}",
                "RUN_ID": "gh-123-1",
            }

            ansi_probe = subprocess.run(
                [
                    str(terraform),
                    "-chdir=infrastructure",
                    "state",
                    "list",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual("\x1b[0m", ansi_probe.stdout)

            wrapped_bin = temp / "wrapped-bin"
            wrapped_bin.mkdir()
            wrapper = wrapped_bin / "terraform"
            wrapper.write_text(
                f"""#!/usr/bin/env bash
set +e
{terraform} "$@"
status=$?
set -e
printf '%s\\n' \\
  '::set-output name=stdout::' \\
  '::set-output name=stderr::' \\
  "::set-output name=exitcode::${{status}}"
exit "${{status}}"
"""
            )
            wrapper.chmod(0o755)
            wrapped = subprocess.run(
                ["bash", "-c", body],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp,
                env={
                    **env,
                    "PATH": f"{wrapped_bin}:{os.environ['PATH']}",
                },
            )
            self.assertNotEqual(0, wrapped.returncode)
            self.assertIn(
                "::set-output name=stdout::",
                wrapped.stderr,
            )

            fresh = subprocess.run(
                ["bash", "-c", body],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp,
                env=env,
            )
            self.assertEqual(0, fresh.returncode, fresh.stderr)

            failed = subprocess.run(
                ["bash", "-c", body],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp,
                env={**env, "FAKE_TERRAFORM_ERROR": "1"},
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertIn("backend unavailable", failed.stderr)

    def test_forced_cleanup_is_explicit_and_never_global(self):
        contents = workflow("benchmark-cleanup.yml")
        self.assertIn("A validated run_id is required for forced cleanup.", contents)
        self.assertIn('--force-run-id "${REQUESTED_RUN_ID}"', contents)
        self.assertIn('--requested-run-id "${REQUESTED_RUN_ID}"', contents)
        self.assertNotIn("4102444800", contents)
        cleanup = job_blocks(contents)["cleanup"]
        self.assertIn(
            "Recheck exact reservation after acquiring run lock", cleanup
        )
        self.assertIn("reservation-exists", cleanup)
        self.assertIn(
            "steps.reservation.outputs.active == 'true'", cleanup
        )
        self.assertLess(
            cleanup.index("Recheck exact reservation after acquiring run lock"),
            cleanup.index("Verify run identity and fetch trusted recovery inputs"),
        )

    def test_recovery_uses_current_validators_and_exact_provisioning_source(self):
        for filename in ("benchmark-cleanup.yml", "terraform-cd.yml"):
            contents = workflow(filename)
            with self.subTest(workflow=filename):
                self.assertIn("--metadata-json", contents)
                self.assertIn("active-metadata.json", contents)
                self.assertIn("validate-provisioning-commit", contents)
                self.assertIn(".head_commit =", contents)
                self.assertIn(
                    "/commits/${CURRENT_MAIN_COMMIT}", contents
                )
                self.assertIn("current-main-commit.json", contents)
                self.assertIn(
                    'url="https://codeload.github.com/${REPO}/tar.gz/${PROVISIONING_COMMIT}"',
                    contents,
                )
                self.assertNotIn("make terraform-init-backend", contents)
                self.assertNotIn("-chdir=infrastructure", contents)
                self.assertNotRegex(
                    contents,
                    r"python3\s+[^\n]*provisioning-source[^\n]*scripts/",
                )
                terraform_commands = re.findall(
                    r"terraform\s+-chdir=([^\s]+)", contents
                )
                self.assertTrue(terraform_commands)
                self.assertEqual(
                    {"provisioning-source/infrastructure"},
                    set(terraform_commands),
                )
                self.assertIn(
                    '${GITHUB_WORKSPACE}/scripts/benchmark_run_state.py',
                    contents,
                )
                self.assertLess(
                    contents.index(
                        "Prove provisioning commit belongs to current main"
                    ),
                    contents.index("Fetch exact provisioning source"),
                )
                self.assertLess(
                    contents.index("Fetch exact provisioning source"),
                    contents.index("Initialize"),
                )
                self.assertLess(
                    contents.index("validate-plan"),
                    contents.index("apply -auto-approve"),
                )

    def test_release_validates_identity_and_never_inlines_matrix_in_shell(self):
        contents = workflow("benchmark-release.yml")
        blocks = job_blocks(contents)
        discover = blocks["discover"]
        release = blocks["release"]
        self.assertLess(
            release.index("Revalidate release identity before credentials"),
            release.index("Configure AWS credentials"),
        )
        self.assertIn("validate_run_id", blocks["discover"])
        self.assertIn("validate_benchmark_uuid", blocks["discover"])
        self.assertIn("validate_benchmark_hours", blocks["discover"])
        self.assertIn(
            "exclude_fuzzers: ${{ steps.release_inputs.outputs.exclude_fuzzers }}",
            discover,
        )
        self.assertEqual(1, contents.count("inputs.exclude_fuzzers"))
        self.assertLess(
            discover.index("Validate and canonicalize release inputs"),
            discover.index("Configure AWS credentials"),
        )
        self.assertIn("normalize-excluded-fuzzers", discover)
        self.assertLess(
            release.index(
                "Revalidate canonical release inputs before credentials"
            ),
            release.index("Configure AWS credentials"),
        )
        self.assertIn("--require-canonical", release)
        run_analysis = release[
            release.index("- name: Run analysis") :
            release.index("- name: Collect analysis artifacts")
        ]
        self.assertIn(
            "EXCLUDE_FUZZERS: ${{ needs.discover.outputs.exclude_fuzzers }}",
            run_analysis,
        )
        self.assertNotIn("inputs.exclude_fuzzers", run_analysis)
        self.assertIn(
            "group: scfuzzbench-release-${{ matrix.run_id }}-${{ matrix.benchmark_uuid }}",
            release,
        )
        self.assertIn("cancel-in-progress: false", release)
        self.assertNotIn("queue:", release)
        for body in run_bodies(contents):
            with self.subTest(body=body[:80]):
                self.assertNotIn("${{ matrix.", body)


if __name__ == "__main__":
    unittest.main()
