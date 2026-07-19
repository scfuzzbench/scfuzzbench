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
  description = "Commit hash for the scfuzzbench repo (used in benchmark UUID)."
  default     = ""
}

variable "benchmark_type" {
  type        = string
  description = "Benchmark type: property (default) or optimization."
  default     = "property"
}

variable "foundry_version" {
  type        = string
  description = "Pinned Foundry version (tag used by foundryup). Only used when foundry_git_repo is empty."
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
  description = "Run identifier (defaults to unix timestamp at apply time)."
  default     = ""
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
}

variable "fuzzer_env" {
  type        = map(string)
  description = "Fuzzer environment variable overrides passed to fuzzer run scripts."
  default     = {}

  validation {
    condition = length(setintersection(toset(keys(var.fuzzer_env)), toset([
      "ECHIDNA_CI_TOKEN",
      "ECHIDNA_CI_TOKEN_SSM_PARAMETER",
      "ECHIDNA_CI_REPO",
      "ECHIDNA_CI_RUN_ID",
      "ECHIDNA_CI_ARTIFACT_NAME",
      "ECHIDNA_CI_ARTIFACT_SHA256",
      "ECHIDNA_CI_COMMIT",
      "ECHIDNA_CI_TOKEN_KMS_KEY_ARN",
      "MEDUSA_GIT_REPO",
      "MEDUSA_GIT_REF",
      "MEDUSA_GIT_COMMIT",
      "MEDUSA_GO_VERSION",
      "MEDUSA_GO_SHA256",
    ]))) == 0
    error_message = "Bleeding-edge tool settings must use their dedicated variables, not fuzzer_env."
  }
}
