variable "aws_region" {
  type        = string
  description = "AWS region to deploy into."
  default     = "us-east-1"
}

variable "ubuntu_ami_ssm_parameter" {
  type        = string
  description = "SSM parameter name for the Ubuntu LTS AMI ID."
  default     = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type for fuzzing nodes."
  default     = "c6a.8xlarge"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC."
  default     = "10.10.0.0/16"
}

variable "public_subnet_cidr" {
  type        = string
  description = "CIDR block for the public subnet."
  default     = "10.10.1.0/24"
}

variable "availability_zone" {
  type        = string
  description = "Optional AZ for the public subnet. If unset, pick an AZ that supports instance_type."
  default     = ""
}

variable "instances_per_fuzzer" {
  type        = number
  description = "Number of parallel instances per fuzzer."
  default     = 10
}

variable "timeout_hours" {
  type        = number
  description = "Timeout for each fuzzer run in hours."
  default     = 24
}

variable "preliminary_interval_seconds" {
  type        = number
  description = "Interval between preliminary log checkpoints. Zero disables preliminary results."
  default     = 3600

  validation {
    condition = (
      var.preliminary_interval_seconds == floor(var.preliminary_interval_seconds) &&
      (
        var.preliminary_interval_seconds == 0 ||
        var.preliminary_interval_seconds >= 60
      ) &&
      var.preliminary_interval_seconds <= 86400
    )
    error_message = "preliminary_interval_seconds must be zero or a whole number in [60, 86400]."
  }
}

variable "target_repo_url" {
  type        = string
  description = "Target repository URL."
  default     = ""
}

variable "target_commit" {
  type        = string
  description = "Target repository commit hash."
  default     = ""
}

variable "scfuzzbench_commit" {
  type        = string
  description = "Full lowercase immutable commit SHA for the canonical scfuzzbench repository."
  default     = ""

  validation {
    condition     = var.scfuzzbench_commit == "" || can(regex("^[0-9a-f]{40}$", var.scfuzzbench_commit))
    error_message = "scfuzzbench_commit must be blank during static validation or a full lowercase 40-character commit SHA."
  }
}

variable "scfuzzbench_repository" {
  type        = string
  description = "Canonical public scfuzzbench repository used for the immutable EC2 bootstrap."
  default     = "https://github.com/scfuzzbench/scfuzzbench"

  validation {
    condition     = var.scfuzzbench_repository == "https://github.com/scfuzzbench/scfuzzbench"
    error_message = "scfuzzbench_repository must be https://github.com/scfuzzbench/scfuzzbench."
  }
}

variable "benchmark_type" {
  type        = string
  description = "Benchmark type: property (default) or optimization."
  default     = "property"
}

variable "foundry_version" {
  type        = string
  description = "Foundry release tag used by foundryup only when foundry_git_repo is empty. Cloud workflows do not expose this local/manual fallback."
  default     = "v1.7.1"
}

variable "foundry_git_repo" {
  type        = string
  description = "Git repository to build Foundry from. Defaults to upstream foundry-rs/foundry."
  default     = "https://github.com/foundry-rs/foundry"
}

variable "foundry_git_ref" {
  type        = string
  description = "Git ref (branch, tag, or commit) for the Foundry repo. Pinned to a master commit that includes invariant assertion-failure reporting (foundry-rs/foundry#14275), continuous invariant campaigns with handler-bug dedup (foundry-rs/foundry#14482), mid-transaction interrupt handling plus mid-run handler-assertion failure events (foundry-rs/foundry#15689, fixes #15684), and automatic invariant worker defaulting for forge test (foundry-rs/foundry#15726); stable releases up to v1.7.1 predate #14482."
  default     = "02c05d970d2801da0aef8b82486ce84b01ede36d"
}

variable "echidna_version" {
  type        = string
  description = "Pinned Echidna version."
  default     = "2.3.1"
}

variable "echidna_ci_repo" {
  type        = string
  description = "Public GitHub repository whose completed Actions run produced an opt-in Echidna Linux artifact. Blank keeps the stable release path."
  default     = ""

  validation {
    condition     = var.echidna_ci_repo == "" || can(regex("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\\.git)?/?$", var.echidna_ci_repo))
    error_message = "echidna_ci_repo must be blank or https://github.com/<org>/<repo>."
  }
}

variable "echidna_ci_run_id" {
  type        = string
  description = "GitHub Actions run ID for the opt-in Echidna artifact."
  default     = ""

  validation {
    condition     = var.echidna_ci_run_id == "" || can(regex("^[1-9][0-9]*$", var.echidna_ci_run_id))
    error_message = "echidna_ci_run_id must be blank or a positive integer."
  }
}

variable "echidna_ci_artifact_name" {
  type        = string
  description = "Exact Linux artifact name within echidna_ci_run_id."
  default     = ""

  validation {
    condition     = var.echidna_ci_artifact_name == "" || can(regex("^[A-Za-z0-9._-]+$", var.echidna_ci_artifact_name))
    error_message = "echidna_ci_artifact_name may contain only letters, digits, dot, underscore, and dash."
  }
}

variable "echidna_ci_artifact_sha256" {
  type        = string
  description = "Expected SHA-256 of the GitHub Actions artifact ZIP (the API digest without its sha256: prefix)."
  default     = ""

  validation {
    condition     = var.echidna_ci_artifact_sha256 == "" || can(regex("^[A-Fa-f0-9]{64}$", var.echidna_ci_artifact_sha256))
    error_message = "echidna_ci_artifact_sha256 must be blank or a 64-character SHA-256 digest."
  }
}

variable "echidna_ci_commit" {
  type        = string
  description = "Full immutable head commit expected for echidna_ci_run_id."
  default     = ""

  validation {
    condition     = var.echidna_ci_commit == "" || can(regex("^[A-Fa-f0-9]{40}$", var.echidna_ci_commit))
    error_message = "echidna_ci_commit must be blank or a full 40-character commit SHA."
  }
}

variable "echidna_ci_token_ssm_parameter_name" {
  type        = string
  description = "SecureString parameter containing a token with Actions read access. Only the parameter name enters Terraform state."
  default     = ""

  validation {
    condition     = var.echidna_ci_token_ssm_parameter_name == "" || can(regex("^/scfuzzbench/[A-Za-z0-9_./-]+$", var.echidna_ci_token_ssm_parameter_name))
    error_message = "echidna_ci_token_ssm_parameter_name must be blank or start with /scfuzzbench/."
  }
}

variable "echidna_ci_token_kms_key_arn" {
  type        = string
  description = "Optional exact customer-managed KMS key ARN for the Echidna token SecureString. Blank uses the account's aws/ssm managed key."
  default     = ""

  validation {
    condition     = var.echidna_ci_token_kms_key_arn == "" || can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Fa-f0-9-]{36}$", var.echidna_ci_token_kms_key_arn))
    error_message = "echidna_ci_token_kms_key_arn must be blank or an exact customer-managed KMS key ARN (aliases and wildcards are not accepted)."
  }
}

variable "medusa_version" {
  type        = string
  description = "Pinned Medusa version."
  default     = "1.4.1"
}

variable "medusa_git_repo" {
  type        = string
  description = "Public GitHub repository for an opt-in Medusa source build. Blank keeps the stable release path."
  default     = ""

  validation {
    condition     = var.medusa_git_repo == "" || can(regex("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\\.git)?/?$", var.medusa_git_repo))
    error_message = "medusa_git_repo must be blank or https://github.com/<org>/<repo>."
  }
}

variable "medusa_git_ref" {
  type        = string
  description = "Human-readable Medusa branch/tag/ref that must still resolve to medusa_git_commit."
  default     = ""

  validation {
    condition     = var.medusa_git_ref == "" || can(regex("^[A-Za-z0-9._/-]+$", var.medusa_git_ref))
    error_message = "medusa_git_ref contains unsupported characters."
  }
}

variable "medusa_git_commit" {
  type        = string
  description = "Full immutable Medusa source commit expected for medusa_git_ref."
  default     = ""

  validation {
    condition     = var.medusa_git_commit == "" || can(regex("^[A-Fa-f0-9]{40}$", var.medusa_git_commit))
    error_message = "medusa_git_commit must be blank or a full 40-character commit SHA."
  }
}

variable "medusa_go_version" {
  type        = string
  description = "Pinned official Go toolchain used only for Medusa source builds."
  default     = "1.24.0"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+(\\.[0-9]+)?$", var.medusa_go_version))
    error_message = "medusa_go_version must look like 1.24.0."
  }
}

variable "medusa_go_sha256" {
  type        = string
  description = "SHA-256 for go<medusa_go_version>.linux-amd64.tar.gz."
  default     = "dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858"

  validation {
    condition     = can(regex("^[A-Fa-f0-9]{64}$", var.medusa_go_sha256))
    error_message = "medusa_go_sha256 must be a 64-character SHA-256 digest."
  }
}

variable "recon_version" {
  type        = string
  description = "Pinned Recon fuzzer version."
  default     = "0.4.6"
}

variable "git_token_ssm_parameter_name" {
  type        = string
  description = "SSM parameter name for a Git token used to clone private target repos."
  default     = ""
}


variable "root_volume_size_gb" {
  type        = number
  description = "Root volume size in GB."
  default     = 100
}

variable "ssh_cidr" {
  type        = string
  description = "CIDR allowed to SSH into instances."
  default     = "0.0.0.0/0"
}

variable "bucket_name_prefix" {
  type        = string
  description = "Prefix for the S3 bucket name when creating a new bucket."
  default     = "scfuzzbench-logs"
}

variable "existing_bucket_name" {
  type        = string
  description = "Use an existing S3 bucket name instead of creating one."
  default     = ""
}

variable "bucket_force_destroy" {
  type        = bool
  description = "Allow Terraform to destroy non-empty bucket."
  default     = false
}

variable "bucket_public_read" {
  type        = bool
  description = "Allow public read access to all objects in the logs bucket."
  default     = true
}

variable "run_id" {
  type        = string
  description = "Immutable run identifier. CI sets this before backend initialization."
  default     = ""

  validation {
    condition     = var.run_id == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", var.run_id))
    error_message = "run_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$."
  }
}

variable "run_started_at_epoch" {
  type        = number
  description = "Unix timestamp generated with the immutable run identity before Terraform initialization."
  default     = 0

  validation {
    condition     = var.run_started_at_epoch >= 0 && floor(var.run_started_at_epoch) == var.run_started_at_epoch
    error_message = "run_started_at_epoch must be a non-negative integer."
  }
}

variable "terraform_backend_key" {
  type        = string
  description = "Run-scoped remote-state key recorded in benchmark provenance."
  default     = ""

  validation {
    condition     = var.terraform_backend_key == "" || can(regex("^runs/[A-Za-z0-9][A-Za-z0-9._-]{0,79}/terraform\\.tfstate$", var.terraform_backend_key))
    error_message = "terraform_backend_key must use runs/<run_id>/terraform.tfstate."
  }
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to AWS resources."
  default = {
    Project = "scfuzzbench"
  }
}

variable "custom_fuzzer_definitions" {
  type = list(object({
    key          = string
    install_path = string
    run_path     = string
  }))
  description = "Additional fuzzer definitions to include (local only)."
  default     = []

  validation {
    condition     = length(var.custom_fuzzer_definitions) == 0
    error_message = "custom_fuzzer_definitions are local-only and cannot be deployed through the cloud Terraform module."
  }

  validation {
    condition = (
      length(var.custom_fuzzer_definitions) ==
      length(distinct([for fuzzer in var.custom_fuzzer_definitions : fuzzer.key]))
      ) && alltrue([
        for fuzzer in var.custom_fuzzer_definitions :
        can(regex("^[a-z0-9][a-z0-9-]{0,63}$", fuzzer.key)) &&
        !contains(["echidna", "foundry", "medusa", "recon-fuzzer"], fuzzer.key)
    ])
    error_message = "Custom fuzzer keys must be unique, must match ^[a-z0-9][a-z0-9-]{0,63}$, and must not shadow built-in fuzzers."
  }

  validation {
    condition = alltrue([
      for fuzzer in var.custom_fuzzer_definitions :
      can(regex("^fuzzers/[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*/install\\.sh$", fuzzer.install_path)) &&
      can(regex("^fuzzers/[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*/run\\.sh$", fuzzer.run_path)) &&
      length(regexall("(^|/)\\.\\.?(/|$)", fuzzer.install_path)) == 0 &&
      length(regexall("(^|/)\\.\\.?(/|$)", fuzzer.run_path)) == 0
    ])
    error_message = "Custom fuzzer scripts must be safe repo-relative fuzzers/.../install.sh and fuzzers/.../run.sh paths."
  }
}

variable "fuzzers" {
  type        = list(string)
  description = "Fuzzer keys to include in the run. Empty means all available fuzzers."
  default     = []

  validation {
    condition = length(var.fuzzers) == length(distinct(var.fuzzers)) && alltrue([
      for fuzzer in var.fuzzers :
      can(regex("^[a-z0-9][a-z0-9-]{0,63}$", fuzzer))
    ])
    error_message = "fuzzers must contain unique fuzzer keys matching ^[a-z0-9][a-z0-9-]{0,63}$."
  }

  validation {
    condition = alltrue([
      for fuzzer in var.fuzzers :
      contains(["echidna", "foundry", "medusa", "recon-fuzzer"], fuzzer)
    ])
    error_message = "fuzzers may contain only the built-in cloud fuzzers: echidna, foundry, medusa, and recon-fuzzer."
  }
}

variable "fuzzer_env" {
  type        = map(string)
  description = "Fuzzer environment variable overrides passed to fuzzer run scripts."
  default     = {}

  validation {
    condition     = length(var.fuzzer_env) <= 64
    error_message = "fuzzer_env must contain at most 64 entries."
  }

  validation {
    condition = sum(concat(
      [0],
      [
        for key, value in var.fuzzer_env :
        (length(base64encode(key)) * 3 / 4) -
        length(regexall("=", base64encode(key))) +
        (length(base64encode(value)) * 3 / 4) -
        length(regexall("=", base64encode(value)))
      ]
    )) <= 4096
    error_message = "fuzzer_env keys and values must contain at most 4096 aggregate UTF-8 bytes."
  }

  validation {
    condition = alltrue([
      for key in keys(var.fuzzer_env) :
      can(regex("^[A-Z][A-Z0-9_]{0,63}$", key))
    ])
    error_message = "fuzzer_env keys must match ^[A-Z][A-Z0-9_]{0,63}$."
  }

  validation {
    condition = alltrue([
      for value in values(var.fuzzer_env) :
      length(value) <= 2000 &&
      length(regexall("[\\r\\n\"`$\\\\]", value)) == 0
    ])
    error_message = "fuzzer_env values must be at most 2000 characters and cannot contain CR, LF, double quotes, backticks, dollar signs, or backslashes."
  }

  validation {
    condition = length(setintersection(toset(keys(var.fuzzer_env)), toset([
      "ECHIDNA_VERSION",
      "ECHIDNA_CI_TOKEN",
      "ECHIDNA_CI_TOKEN_SSM_PARAMETER",
      "ECHIDNA_CI_REPO",
      "ECHIDNA_CI_RUN_ID",
      "ECHIDNA_CI_ARTIFACT_NAME",
      "ECHIDNA_CI_ARTIFACT_SHA256",
      "ECHIDNA_CI_COMMIT",
      "ECHIDNA_CI_TOKEN_KMS_KEY_ARN",
      "FOUNDRY_GIT_REF",
      "FOUNDRY_GIT_REPO",
      "FOUNDRY_VERSION",
      "SCFUZZBENCH_FOUNDRY_SOURCE_PATCH",
      "MEDUSA_VERSION",
      "MEDUSA_GIT_REPO",
      "MEDUSA_GIT_REF",
      "MEDUSA_GIT_COMMIT",
      "MEDUSA_GO_VERSION",
      "MEDUSA_GO_SHA256",
      "RECON_VERSION",
    ]))) == 0
    error_message = "Tool settings must use their dedicated variables, not fuzzer_env."
  }

  validation {
    condition = alltrue([
      for key in keys(var.fuzzer_env) :
      !contains([
        "SCFUZZBENCH_SEED_CORPUS_SOURCE",
        "SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE",
        "SCFUZZBENCH_SEED_CORPUS_HELPER",
        "SCFUZZBENCH_SEED_CORPUS_MAX_BYTES",
        "SCFUZZBENCH_SEED_CORPUS_MAX_FILES",
        "SCFUZZBENCH_SEED_CORPUS_METADATA_PATH",
        "SCFUZZBENCH_PROPERTIES_PATH",
      ], key)
    ])
    error_message = "fuzzer_env cannot override framework-owned benchmark variables."
  }

  validation {
    condition = length(setintersection(toset(keys(var.fuzzer_env)), toset([
      "AWS_ACCESS_KEY_ID",
      "AWS_DEFAULT_REGION",
      "AWS_REGION",
      "AWS_SECRET_ACCESS_KEY",
      "AWS_SESSION_TOKEN",
      "SCFUZZBENCH_AWS_CREDS_ENV_FILE",
      "SCFUZZBENCH_AWS_CREDS_REFRESH_PID",
      "SCFUZZBENCH_AWS_CREDS_REFRESH_PID_START_TICKS",
      "SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS",
      "SCFUZZBENCH_BENCHMARK_MANIFEST_B64",
      "SCFUZZBENCH_BENCHMARK_TYPE",
      "SCFUZZBENCH_BENCHMARK_UUID",
      "SCFUZZBENCH_BIN_DIR",
      "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION",
      "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH",
      "SCFUZZBENCH_COMMIT",
      "SCFUZZBENCH_COMMON_SH",
      "SCFUZZBENCH_CORPUS_DIR",
      "SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE",
      "SCFUZZBENCH_FUZZER_KEY",
      "SCFUZZBENCH_FUZZER_LABEL",
      "SCFUZZBENCH_GIT_TOKEN",
      "SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER",
      "SCFUZZBENCH_INSTANCE_ID",
      "SCFUZZBENCH_LOCAL_MODE",
      "SCFUZZBENCH_LOG_DIR",
      "SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS",
      "SCFUZZBENCH_PRELIMINARY_PID",
      "SCFUZZBENCH_PRELIMINARY_PID_START_TICKS",
      "SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT",
      "SCFUZZBENCH_REPO_URL",
      "SCFUZZBENCH_ROOT",
      "SCFUZZBENCH_RUNNER_METRICS_PID",
      "SCFUZZBENCH_RUNNER_METRICS_PID_START_TICKS",
      "SCFUZZBENCH_RUN_HEARTBEAT_SECONDS",
      "SCFUZZBENCH_RUN_ID",
      "SCFUZZBENCH_RUN_INDEX",
      "SCFUZZBENCH_RUN_STARTED_AT_EPOCH",
      "SCFUZZBENCH_S3_BUCKET",
      "SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS",
      "SCFUZZBENCH_TIMEOUT_GRACE_SECONDS",
      "SCFUZZBENCH_TIMEOUT_SECONDS",
      "SCFUZZBENCH_UPLOAD_DONE",
      "SCFUZZBENCH_WORKDIR",
      "SCFUZZBENCH_WORKERS_RESOLVED",
    ]))) == 0
    error_message = "fuzzer_env cannot override immutable runner identity, credentials, paths, or timing."
  }

  validation {
    condition = alltrue([
      for key in keys(var.fuzzer_env) : !startswith(key, "AWS_")
    ])
    error_message = "fuzzer_env cannot set AWS SDK credential, endpoint, or configuration variables."
  }

  validation {
    condition = alltrue([
      for key in keys(var.fuzzer_env) :
      !startswith(key, "SCFUZZBENCH_") || contains([
        "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS",
        "SCFUZZBENCH_FOUNDRY_SHOWMAP",
        "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS",
        "SCFUZZBENCH_RUNNER_METRICS",
        "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS",
        "SCFUZZBENCH_WORKERS",
      ], key)
    ])
    error_message = "fuzzer_env may set only the documented safe SCFUZZBENCH_* tuning variables."
  }

  validation {
    condition = alltrue([
      for key, value in var.fuzzer_env :
      !contains([
        "SCFUZZBENCH_FOUNDRY_SHOWMAP",
        "SCFUZZBENCH_RUNNER_METRICS",
        ], key) || contains(
        ["0", "1", "false", "no", "off", "on", "true", "yes"],
        lower(value),
      )
    ])
    error_message = "SCFUZZBENCH_FOUNDRY_SHOWMAP and SCFUZZBENCH_RUNNER_METRICS must be boolean values."
  }

  validation {
    condition = alltrue([
      for key, value in var.fuzzer_env :
      key != "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS" || contains(["0", "1"], value)
    ])
    error_message = "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS must be 0 or 1."
  }

  validation {
    condition = alltrue([
      for key, value in var.fuzzer_env :
      !contains([
        "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS",
        "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS",
        "SCFUZZBENCH_WORKERS",
        ], key) || (
        can(regex("^[0-9]+$", value)) &&
        try(tonumber(value) >= 1, false) &&
        try(tonumber(value) <= (
          key == "SCFUZZBENCH_WORKERS" ? 256 :
          key == "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS" ? 300 :
          3600
        ), false)
      )
    ])
    error_message = "Safe SCFUZZBENCH integer tunings are outside their allowed bounds."
  }

  validation {
    condition = alltrue([
      for key, value in var.fuzzer_env :
      !contains([
        "ECHIDNA_CORPUS_DIR",
        "FOUNDRY_CORPUS_DIR",
        "MEDUSA_CORPUS_DIR",
        "RECON_CORPUS_DIR",
        ], key) || value == "" || (
        !startswith(value, "/") &&
        can(regex("^[A-Za-z0-9_.+/-]+$", value)) &&
        length(regexall("(^|/)\\.\\.?(/|$)", value)) == 0
      )
    ])
    error_message = "Fuzzer corpus directory overrides must be safe repo-relative paths without '.' or '..' segments."
  }
}

variable "properties_path" {
  type        = string
  description = "Optional repo-relative properties path for benchmark mode switching."
  default     = ""

  validation {
    condition = var.properties_path == "" || (
      !startswith(var.properties_path, "/") &&
      can(regex("^[A-Za-z0-9_./-]+$", var.properties_path)) &&
      length(regexall("(^|/)\\.\\.?(/|$)", var.properties_path)) == 0
    )
    error_message = "properties_path must be empty or a safe repo-relative path without '.' or '..' segments."
  }
}

variable "shared_seed_corpus_source" {
  type        = string
  description = "Optional shared seed corpus directory or s3://bucket/prefix. Relative directories resolve inside the cloned target; absolute paths must exist on each runner."
  default     = ""

  validation {
    condition = var.shared_seed_corpus_source == "" || (
      can(regex("^(s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/)?[A-Za-z0-9/._+~-]+/?$", var.shared_seed_corpus_source)) &&
      length(regexall("(^|/)\\.\\.?(/|$)", var.shared_seed_corpus_source)) == 0
    )
    error_message = "shared_seed_corpus_source must be empty, a safe local path, or an s3://bucket/prefix without '.'/'..' segments, spaces, query parameters, or shell metacharacters."
  }
}
