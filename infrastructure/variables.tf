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
  description = "Pinned Foundry version (tag used by foundryup)."
  default     = "v1.6.0-rc1"
}

variable "foundry_git_repo" {
  type        = string
  description = "Optional git repository to build Foundry from."
  default     = "https://github.com/0xalpharush/foundry"
}

variable "foundry_git_ref" {
  type        = string
  description = "Optional git ref (branch, tag, or commit) for Foundry repo."
  default     = "fail_on_assert"
}

variable "echidna_version" {
  type        = string
  description = "Pinned Echidna version."
  default     = "2.3.1"
}

variable "medusa_version" {
  type        = string
  description = "Pinned Medusa version."
  default     = "1.4.1"
}

variable "bitwuzla_version" {
  type        = string
  description = "Pinned Bitwuzla version for symexec fallback install."
  default     = "0.8.2"
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
}
