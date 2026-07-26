locals {
  timeout_seconds      = var.timeout_hours * 3600
  run_id               = var.run_id != "" ? var.run_id : time_static.run.unix
  run_started_at_epoch = var.run_started_at_epoch != 0 ? var.run_started_at_epoch : time_static.run.unix
  # The AWS provider marks every SSM value sensitive. This value is retrieved
  # without SecureString decryption and validated as an AMI ID before use.
  ubuntu_ami_id = nonsensitive(data.aws_ssm_parameter.ubuntu_ami.value)

  echidna_ci_inputs = [
    var.echidna_ci_repo,
    var.echidna_ci_run_id,
    var.echidna_ci_artifact_name,
    var.echidna_ci_artifact_sha256,
    var.echidna_ci_commit,
    var.echidna_ci_token_ssm_parameter_name,
  ]
  echidna_ci_input_count = length(compact(local.echidna_ci_inputs))
  echidna_ci_enabled     = local.echidna_ci_input_count > 0

  medusa_source_inputs = [
    var.medusa_git_repo,
    var.medusa_git_ref,
    var.medusa_git_commit,
  ]
  medusa_source_input_count = length(compact(local.medusa_source_inputs))
  medusa_source_enabled     = local.medusa_source_input_count > 0

  foundry_throughput_patch_ref    = "61f4ab717410ffc858b5000a909c805de47d6c5f"
  foundry_throughput_patch_path   = "${path.module}/../fuzzers/foundry/throughput-progress.patch"
  foundry_throughput_patch_sha256 = filesha256(local.foundry_throughput_patch_path)
  foundry_source_patch = var.foundry_git_repo != "" && var.foundry_git_ref == local.foundry_throughput_patch_ref ? (
    "scfuzzbench-throughput-progress-v1@sha256:${local.foundry_throughput_patch_sha256}"
  ) : ""

  bootstrap_file_destinations = {
    "fuzzers/_shared/common.sh"                 = "common.sh"
    "fuzzers/_shared/prepare_seed_corpus.py"    = "prepare_seed_corpus.py"
    "fuzzers/_shared/safe_path_ops.py"          = "safe_path_ops.py"
    "fuzzers/_shared/put_manifest_once.py"      = "put_manifest_once.py"
    "fuzzers/_shared/upload_pinned_file.py"     = "upload_pinned_file.py"
    "infrastructure/bootstrap_bundle.py"        = "bootstrap_bundle.py"
    "infrastructure/bootstrap_source_guard.py"  = "provenance/bootstrap_source_guard.py"
    "infrastructure/user_data.sh.tftpl"         = "provenance/user_data.sh.tftpl"
    "scripts/preliminary_snapshot.py"           = "preliminary_snapshot.py"
    "fuzzers/echidna/install.sh"                = "fuzzers/echidna/install.sh"
    "fuzzers/echidna/run.sh"                    = "fuzzers/echidna/run.sh"
    "fuzzers/echidna/extract_ci_artifact.py"    = "extract_echidna_ci_artifact.py"
    "fuzzers/medusa/install.sh"                 = "fuzzers/medusa/install.sh"
    "fuzzers/medusa/run.sh"                     = "fuzzers/medusa/run.sh"
    "fuzzers/medusa/extract_go_toolchain.py"    = "extract_go_toolchain.py"
    "fuzzers/foundry/install.sh"                = "fuzzers/foundry/install.sh"
    "fuzzers/foundry/run.sh"                    = "fuzzers/foundry/run.sh"
    "fuzzers/foundry/throughput-progress.patch" = "foundry-throughput-progress.patch"
    "fuzzers/recon-fuzzer/install.sh"           = "fuzzers/recon-fuzzer/install.sh"
    "fuzzers/recon-fuzzer/run.sh"               = "fuzzers/recon-fuzzer/run.sh"
  }
  bootstrap_files = {
    for source, destination in local.bootstrap_file_destinations : source => {
      destination = destination
      executable  = endswith(source, ".sh") || endswith(source, ".py")
      sha256      = filesha256("${path.module}/../${source}")
    }
  }
  bootstrap_manifest = {
    schema_version = 1
    repository     = var.scfuzzbench_repository
    commit         = lower(var.scfuzzbench_commit)
    files          = local.bootstrap_files
  }
  bootstrap_manifest_json   = jsonencode(local.bootstrap_manifest)
  bootstrap_manifest_b64    = base64encode(local.bootstrap_manifest_json)
  bootstrap_manifest_sha256 = sha256(local.bootstrap_manifest_json)
  bootstrap_installer_sha256 = local.bootstrap_files[
    "infrastructure/bootstrap_bundle.py"
  ].sha256
  user_data_template_sha256 = local.bootstrap_files[
    "infrastructure/user_data.sh.tftpl"
  ].sha256

  seed_corpus_s3_match  = regexall("^s3://([^/]+)/(.+?)/?$", var.shared_seed_corpus_source)
  seed_corpus_s3_parts  = length(local.seed_corpus_s3_match) == 1 ? local.seed_corpus_s3_match[0] : ["", ""]
  seed_corpus_s3_bucket = local.seed_corpus_s3_parts[0]
  seed_corpus_s3_prefix = trimsuffix(local.seed_corpus_s3_parts[1], "/")
  seed_corpus_source_type = var.shared_seed_corpus_source == "" ? "" : (
    local.seed_corpus_s3_bucket != "" ? "s3" : "local"
  )
  seed_corpus_local_path = trimsuffix(var.shared_seed_corpus_source, "/")
  seed_corpus_provenance_source = local.seed_corpus_source_type == "s3" ? (
    trimsuffix(var.shared_seed_corpus_source, "/")
    ) : startswith(local.seed_corpus_local_path, "/") ? (
    "local-sha256://${sha256(local.seed_corpus_local_path)}"
    ) : local.seed_corpus_source_type == "local" ? (
    "target://${trimprefix(local.seed_corpus_local_path, "./")}"
  ) : ""

  # A release tag is meaningful only on the foundryup path. Source builds
  # record the version reported by forge when results are uploaded.
  foundry_release_version = var.foundry_git_repo == "" ? var.foundry_version : ""

  # Pick an AZ that supports the requested instance type to avoid flaky applies
  # when AWS auto-selects an AZ where the type isn't offered.
  subnet_availability_zone = var.availability_zone != "" ? var.availability_zone : sort(data.aws_ec2_instance_type_offerings.fuzzer.locations)[0]

  benchmark_definition = merge({
    scfuzzbench_commit           = var.scfuzzbench_commit
    scfuzzbench_repository       = var.scfuzzbench_repository
    bootstrap_manifest_sha256    = local.bootstrap_manifest_sha256
    bootstrap_installer_sha256   = local.bootstrap_installer_sha256
    user_data_template_sha256    = local.user_data_template_sha256
    target_repo_url              = var.target_repo_url
    target_commit                = var.target_commit
    benchmark_type               = var.benchmark_type
    instance_type                = var.instance_type
    instances_per_fuzzer         = var.instances_per_fuzzer
    timeout_hours                = var.timeout_hours
    preliminary_interval_seconds = var.preliminary_interval_seconds
    aws_region                   = var.aws_region
    ubuntu_ami_id                = local.ubuntu_ami_id
    foundry_version              = local.foundry_release_version
    foundry_git_repo             = var.foundry_git_repo
    foundry_git_ref              = var.foundry_git_ref
    foundry_source_patch         = local.foundry_source_patch
    echidna_version              = local.echidna_ci_enabled ? "" : var.echidna_version
    echidna_ci_repo              = var.echidna_ci_repo
    echidna_ci_run_id            = var.echidna_ci_run_id
    echidna_ci_artifact          = var.echidna_ci_artifact_name
    echidna_ci_sha256            = lower(var.echidna_ci_artifact_sha256)
    echidna_ci_commit            = lower(var.echidna_ci_commit)
    echidna_ci_token_kms_key_arn = var.echidna_ci_token_kms_key_arn
    medusa_version               = local.medusa_source_enabled ? "" : var.medusa_version
    medusa_git_repo              = var.medusa_git_repo
    medusa_git_ref               = var.medusa_git_ref
    medusa_git_commit            = lower(var.medusa_git_commit)
    medusa_go_version            = local.medusa_source_enabled ? var.medusa_go_version : ""
    medusa_go_sha256             = local.medusa_source_enabled ? lower(var.medusa_go_sha256) : ""
    recon_version                = var.recon_version
    properties_path              = var.properties_path
    fuzzer_keys                  = sort([for fuzzer in local.fuzzer_definitions : fuzzer.key])
    }, var.shared_seed_corpus_source != "" ? {
    seed_corpus = {
      source         = local.seed_corpus_provenance_source
      source_type    = local.seed_corpus_source_type
      copy_semantics = "recursive-byte-for-byte"
    }
  } : {})

  benchmark_definition_json = jsonencode(local.benchmark_definition)
  benchmark_uuid            = md5(local.benchmark_definition_json)
  benchmark_manifest = merge(local.benchmark_definition, {
    run_id                 = local.run_id
    run_started_at_epoch   = local.run_started_at_epoch
    terraform_backend_key  = var.terraform_backend_key
    artifact_prefix        = "logs/${local.run_id}/${local.benchmark_uuid}"
    run_state_metadata_key = "run-state/runs/${local.run_id}/metadata.json"
  })
  benchmark_manifest_json = jsonencode(local.benchmark_manifest)
  benchmark_manifest_b64  = base64encode(local.benchmark_manifest_json)

  # Keep names below IAM's 64-character role limit while retaining a digest of
  # the complete run ID so truncated identities cannot collide.
  run_name_token = substr(replace(lower(tostring(local.run_id)), "/[^a-z0-9-]/", "-"), 0, 18)
  run_name_hash  = substr(sha256(tostring(local.run_id)), 0, 8)
  name_prefix    = "scfuzzbench-${local.run_name_token}-${local.run_name_hash}"
  tags = merge(var.tags, {
    Project       = "scfuzzbench"
    RunId         = tostring(local.run_id)
    BenchmarkUuid = local.benchmark_uuid
    TargetRepo    = substr(var.target_repo_url, 0, 256)
    TargetCommit  = substr(var.target_commit, 0, 256)
  })

  default_fuzzer_env = {
    ECHIDNA_CONFIG     = "echidna.yaml"
    ECHIDNA_TARGET     = "test/recon/CryticTester.sol"
    ECHIDNA_CONTRACT   = "CryticTester"
    ECHIDNA_EXTRA_ARGS = "--test-limit 1000000000"
  }
  properties_path_env = var.properties_path != "" ? {
    SCFUZZBENCH_PROPERTIES_PATH = var.properties_path
  } : {}
  merged_fuzzer_env = merge(
    local.default_fuzzer_env,
    var.fuzzer_env,
    local.properties_path_env,
  )

  base_fuzzer_definitions = [
    {
      key          = "echidna"
      install_path = "fuzzers/echidna/install.sh"
      run_path     = "fuzzers/echidna/run.sh"
    },
    {
      key          = "medusa"
      install_path = "fuzzers/medusa/install.sh"
      run_path     = "fuzzers/medusa/run.sh"
    },
    {
      key          = "foundry"
      install_path = "fuzzers/foundry/install.sh"
      run_path     = "fuzzers/foundry/run.sh"
    },
    {
      key          = "recon-fuzzer"
      install_path = "fuzzers/recon-fuzzer/install.sh"
      run_path     = "fuzzers/recon-fuzzer/run.sh"
    },
  ]
  available_fuzzer_keys = [
    for fuzzer in concat(local.base_fuzzer_definitions, var.custom_fuzzer_definitions) :
    fuzzer.key
  ]
  selected_fuzzer_keys = length(var.fuzzers) > 0 ? toset(var.fuzzers) : toset(local.available_fuzzer_keys)
  fuzzer_definitions = [
    for fuzzer in concat(local.base_fuzzer_definitions, var.custom_fuzzer_definitions) :
    fuzzer if contains(local.selected_fuzzer_keys, fuzzer.key)
  ]
  echidna_ci_selected = local.echidna_ci_enabled && contains([
    for fuzzer in local.fuzzer_definitions : fuzzer.key
  ], "echidna")
  medusa_source_selected = local.medusa_source_enabled && contains([
    for fuzzer in local.fuzzer_definitions : fuzzer.key
  ], "medusa")

  instances = flatten([
    for fuzzer in local.fuzzer_definitions : [
      for index in range(var.instances_per_fuzzer) : {
        key       = "${fuzzer.key}-${index}"
        fuzzer    = fuzzer
        run_index = index
      }
    ]
  ])

  instance_map = { for instance in local.instances : instance.key => instance }

  instance_user_data_rendered = {
    for instance_key, instance in local.instance_map : instance_key => templatefile(
      "${path.module}/user_data.sh.tftpl",
      {
        bootstrap_installer_sha256_b64   = base64encode(local.bootstrap_installer_sha256)
        bootstrap_manifest_b64           = local.bootstrap_manifest_b64
        bootstrap_manifest_sha256_b64    = base64encode(local.bootstrap_manifest_sha256)
        scfuzzbench_repository_b64       = base64encode(var.scfuzzbench_repository)
        scfuzzbench_commit_b64           = base64encode(lower(var.scfuzzbench_commit))
        fuzzer_key_b64                   = base64encode(instance.fuzzer.key)
        aws_region_b64                   = base64encode(var.aws_region)
        s3_bucket_b64                    = base64encode(local.bucket_name)
        run_id_b64                       = base64encode(tostring(local.run_id))
        run_started_at_epoch_b64         = base64encode(tostring(local.run_started_at_epoch))
        benchmark_uuid_b64               = base64encode(local.benchmark_uuid)
        benchmark_manifest_b64_b64       = base64encode(local.benchmark_manifest_b64)
        timeout_seconds_b64              = base64encode(tostring(local.timeout_seconds))
        repo_url_b64                     = base64encode(var.target_repo_url)
        repo_commit_b64                  = base64encode(var.target_commit)
        benchmark_type_b64               = base64encode(var.benchmark_type)
        preliminary_interval_seconds_b64 = base64encode(tostring(var.preliminary_interval_seconds))
        run_index_b64                    = base64encode(tostring(instance.run_index))
        foundry_version_b64              = base64encode(local.foundry_release_version)
        foundry_git_repo_b64             = base64encode(var.foundry_git_repo)
        foundry_git_ref_b64              = base64encode(var.foundry_git_ref)
        echidna_version_b64              = base64encode(var.echidna_version)
        echidna_ci_repo_b64              = base64encode(instance.fuzzer.key == "echidna" ? var.echidna_ci_repo : "")
        echidna_ci_run_id_b64            = base64encode(instance.fuzzer.key == "echidna" ? var.echidna_ci_run_id : "")
        echidna_ci_artifact_name_b64     = base64encode(instance.fuzzer.key == "echidna" ? var.echidna_ci_artifact_name : "")
        echidna_ci_artifact_sha256_b64   = base64encode(instance.fuzzer.key == "echidna" ? var.echidna_ci_artifact_sha256 : "")
        echidna_ci_commit_b64            = base64encode(instance.fuzzer.key == "echidna" ? var.echidna_ci_commit : "")
        echidna_ci_token_ssm_parameter_name_b64 = base64encode(
          instance.fuzzer.key == "echidna" ? var.echidna_ci_token_ssm_parameter_name : ""
        )
        medusa_version_b64 = base64encode(var.medusa_version)
        medusa_git_repo_b64 = base64encode(
          instance.fuzzer.key == "medusa" ? var.medusa_git_repo : ""
        )
        medusa_git_ref_b64 = base64encode(
          instance.fuzzer.key == "medusa" ? var.medusa_git_ref : ""
        )
        medusa_git_commit_b64 = base64encode(
          instance.fuzzer.key == "medusa" ? var.medusa_git_commit : ""
        )
        medusa_go_version_b64 = base64encode(
          instance.fuzzer.key == "medusa" && local.medusa_source_enabled ? var.medusa_go_version : ""
        )
        medusa_go_sha256_b64 = base64encode(
          instance.fuzzer.key == "medusa" && local.medusa_source_enabled ? var.medusa_go_sha256 : ""
        )
        recon_version_b64                 = base64encode(var.recon_version)
        git_token_ssm_parameter_name_b64  = base64encode(var.git_token_ssm_parameter_name)
        seed_corpus_source_b64            = base64encode(var.shared_seed_corpus_source)
        seed_corpus_provenance_source_b64 = base64encode(local.seed_corpus_provenance_source)
        fuzzer_env_b64 = {
          for key, value in local.merged_fuzzer_env : key => base64encode(value)
        }
      }
    )
  }
  instance_user_data_base64 = {
    for instance_key, rendered in local.instance_user_data_rendered :
    instance_key => base64gzip(rendered)
  }
  instance_user_data_gzip_bytes = {
    for instance_key, encoded in local.instance_user_data_base64 :
    instance_key => (length(encoded) * 3 / 4) - length(regexall("=", encoded))
  }
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "time_static" "run" {}

data "aws_ssm_parameter" "ubuntu_ami" {
  name            = var.ubuntu_ami_ssm_parameter
  with_decryption = false

  lifecycle {
    postcondition {
      condition     = can(regex("^ami-[0-9a-f]{8}([0-9a-f]{9})?$", nonsensitive(self.value)))
      error_message = "ubuntu_ami_ssm_parameter must resolve to an AMI ID."
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ec2_instance_type_offerings" "fuzzer" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }

  filter {
    name   = "location"
    values = data.aws_availability_zones.available.names
  }

  location_type = "availability-zone"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  lifecycle {
    precondition {
      condition = (
        var.terraform_backend_key == "" ||
        var.terraform_backend_key == "runs/${local.run_id}/terraform.tfstate"
      )
      error_message = "terraform_backend_key must be derived from the same run_id."
    }
  }

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-vpc"
  })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-igw"
  })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = local.subnet_availability_zone
  map_public_ip_on_launch = true

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-public"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-public"
  })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "ssh" {
  name        = "${local.name_prefix}-ssh"
  description = "SSH access for scfuzzbench instances"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-ssh"
  })
}

resource "aws_s3_bucket" "logs" {
  count         = var.existing_bucket_name == "" ? 1 : 0
  bucket        = "${var.bucket_name_prefix}-${random_id.suffix.hex}"
  force_destroy = var.bucket_force_destroy

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-logs"
  })
}

resource "aws_s3_bucket_public_access_block" "logs" {
  count = length(aws_s3_bucket.logs)

  bucket                  = aws_s3_bucket.logs[count.index].id
  block_public_acls       = !var.bucket_public_read
  block_public_policy     = !var.bucket_public_read
  ignore_public_acls      = !var.bucket_public_read
  restrict_public_buckets = !var.bucket_public_read
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  count = length(aws_s3_bucket.logs)

  bucket = aws_s3_bucket.logs[count.index].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  count = length(aws_s3_bucket.logs)

  bucket = aws_s3_bucket.logs[count.index].id

  versioning_configuration {
    status = "Enabled"
  }
}

locals {
  bucket_name                        = var.existing_bucket_name != "" ? var.existing_bucket_name : try(aws_s3_bucket.logs[0].bucket, "")
  git_token_ssm_parameter_arn        = var.git_token_ssm_parameter_name != "" ? "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.git_token_ssm_parameter_name, "/")}" : ""
  echidna_ci_token_ssm_parameter_arn = var.echidna_ci_token_ssm_parameter_name != "" ? "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.echidna_ci_token_ssm_parameter_name, "/")}" : ""
  ssm_parameter_arns                 = compact([local.git_token_ssm_parameter_arn])
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "ssh" {
  key_name   = "${local.name_prefix}-${random_id.suffix.hex}"
  public_key = tls_private_key.ssh.public_key_openssh

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-key"
  })
}

resource "local_sensitive_file" "ssh_private_key" {
  filename        = "${path.module}/keys/${local.name_prefix}-${random_id.suffix.hex}.pem"
  content         = tls_private_key.ssh.private_key_pem
  file_permission = "0600"
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fuzzer" {
  name               = "${local.name_prefix}-role-${random_id.suffix.hex}"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json

  tags = local.tags
}

data "aws_iam_policy_document" "s3_access" {
  statement {
    sid       = "ReadArtifactBucketLocation"
    actions   = ["s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.bucket_name}"]
  }

  statement {
    sid = "WriteOwnBenchmarkArtifacts"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/logs/${local.run_id}/${local.benchmark_uuid}/*",
      "arn:aws:s3:::${local.bucket_name}/corpus/${local.run_id}/${local.benchmark_uuid}/*",
      "arn:aws:s3:::${local.bucket_name}/runs/${local.run_id}/${local.benchmark_uuid}/manifest.json",
      "arn:aws:s3:::${local.bucket_name}/preliminary/${local.run_id}/${local.benchmark_uuid}/snapshots/*",
      "arn:aws:s3:::${local.bucket_name}/run-state/heartbeats/${local.run_id}/${local.benchmark_uuid}/*",
    ]
  }

  statement {
    sid     = "VerifyOwnImmutableArtifacts"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/logs/${local.run_id}/${local.benchmark_uuid}/*",
      "arn:aws:s3:::${local.bucket_name}/corpus/${local.run_id}/${local.benchmark_uuid}/*",
      "arn:aws:s3:::${local.bucket_name}/runs/${local.run_id}/${local.benchmark_uuid}/manifest.json",
      "arn:aws:s3:::${local.bucket_name}/preliminary/${local.run_id}/${local.benchmark_uuid}/snapshots/*",
    ]
  }

  dynamic "statement" {
    for_each = local.ssm_parameter_arns

    content {
      actions   = ["ssm:GetParameter"]
      resources = [statement.value]
    }
  }

  dynamic "statement" {
    for_each = local.seed_corpus_s3_bucket != "" ? [local.seed_corpus_s3_bucket] : []

    content {
      sid       = "ReadSharedSeedCorpusObjects"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = ["arn:aws:s3:::${statement.value}/${local.seed_corpus_s3_prefix}/*"]
    }
  }

  dynamic "statement" {
    for_each = local.seed_corpus_s3_bucket != "" ? [local.seed_corpus_s3_bucket] : []

    content {
      sid       = "ListSharedSeedCorpusPrefix"
      actions   = ["s3:ListBucket"]
      resources = ["arn:aws:s3:::${statement.value}"]

      condition {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${local.seed_corpus_s3_prefix}/", "${local.seed_corpus_s3_prefix}/*"]
      }
    }
  }
}

data "aws_iam_policy_document" "public_read" {
  count = var.bucket_public_read ? 1 : 0

  statement {
    sid     = "PublicReadAllObjects"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [
      "arn:aws:s3:::${local.bucket_name}/*"
    ]
  }
}

resource "aws_s3_bucket_policy" "public_read" {
  count = var.bucket_public_read && length(aws_s3_bucket.logs) > 0 ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id
  policy = data.aws_iam_policy_document.public_read[0].json
}

resource "aws_iam_role_policy" "s3_access" {
  name   = "${local.name_prefix}-s3-${random_id.suffix.hex}"
  role   = aws_iam_role.fuzzer.id
  policy = data.aws_iam_policy_document.s3_access.json
}

resource "aws_iam_instance_profile" "fuzzer" {
  name = "${local.name_prefix}-profile-${random_id.suffix.hex}"
  role = aws_iam_role.fuzzer.name
  tags = local.tags
}

resource "aws_iam_role" "echidna_ci" {
  count = local.echidna_ci_selected ? 1 : 0

  name               = "${local.name_prefix}-echidna-ci-role-${random_id.suffix.hex}"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = local.tags
}

data "aws_iam_policy_document" "echidna_ci_access" {
  count = local.echidna_ci_selected ? 1 : 0

  source_policy_documents = [data.aws_iam_policy_document.s3_access.json]

  statement {
    sid       = "ReadExactEchidnaToken"
    actions   = ["ssm:GetParameter"]
    resources = [local.echidna_ci_token_ssm_parameter_arn]
  }

  dynamic "statement" {
    for_each = var.echidna_ci_token_kms_key_arn == "" ? [] : [var.echidna_ci_token_kms_key_arn]

    content {
      sid       = "DecryptExactEchidnaTokenKey"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]

      condition {
        test     = "StringEquals"
        variable = "kms:EncryptionContext:PARAMETER_ARN"
        values   = [local.echidna_ci_token_ssm_parameter_arn]
      }

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["ssm.${var.aws_region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "echidna_ci_access" {
  count = local.echidna_ci_selected ? 1 : 0

  name   = "${local.name_prefix}-echidna-ci-${random_id.suffix.hex}"
  role   = aws_iam_role.echidna_ci[0].id
  policy = data.aws_iam_policy_document.echidna_ci_access[0].json
}

resource "aws_iam_instance_profile" "echidna_ci" {
  count = local.echidna_ci_selected ? 1 : 0

  name = "${local.name_prefix}-echidna-ci-profile-${random_id.suffix.hex}"
  role = aws_iam_role.echidna_ci[0].name
  tags = local.tags
}

resource "terraform_data" "bootstrap_source_guard" {
  triggers_replace = [
    lower(var.scfuzzbench_commit),
    local.bootstrap_manifest_sha256,
  ]

  provisioner "local-exec" {
    working_dir = path.module
    command     = <<-EOT
      python3 bootstrap_source_guard.py \
        --repository-root .. \
        --manifest-b64 "$SCFUZZBENCH_BOOTSTRAP_MANIFEST_B64" \
        --manifest-sha256 "$SCFUZZBENCH_BOOTSTRAP_MANIFEST_SHA256" \
        --repository "$SCFUZZBENCH_BOOTSTRAP_REPOSITORY" \
        --commit "$SCFUZZBENCH_BOOTSTRAP_COMMIT"
    EOT
    environment = {
      SCFUZZBENCH_BOOTSTRAP_MANIFEST_B64    = local.bootstrap_manifest_b64
      SCFUZZBENCH_BOOTSTRAP_MANIFEST_SHA256 = local.bootstrap_manifest_sha256
      SCFUZZBENCH_BOOTSTRAP_REPOSITORY      = var.scfuzzbench_repository
      SCFUZZBENCH_BOOTSTRAP_COMMIT          = lower(var.scfuzzbench_commit)
    }
  }
}

resource "aws_instance" "fuzzer" {
  for_each = local.instance_map

  ami                         = local.ubuntu_ami_id
  instance_type               = var.instance_type
  associate_public_ip_address = true
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.ssh.id]
  key_name                    = aws_key_pair.ssh.key_name
  iam_instance_profile = (
    each.value.fuzzer.key == "echidna" && local.echidna_ci_selected
    ? aws_iam_instance_profile.echidna_ci[0].name
    : aws_iam_instance_profile.fuzzer.name
  )
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true

  user_data_base64 = local.instance_user_data_base64[each.key]

  depends_on = [terraform_data.bootstrap_source_guard]

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    tags = merge(local.tags, {
      Name = "${local.name_prefix}-${each.value.fuzzer.key}-${each.value.run_index}-root"
    })
  }

  metadata_options {
    http_tokens = "required"
  }

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9a-f]{40}$", var.scfuzzbench_commit))
      error_message = "scfuzzbench_commit must be a full lowercase immutable commit SHA."
    }

    precondition {
      condition     = var.scfuzzbench_repository == "https://github.com/scfuzzbench/scfuzzbench"
      error_message = "scfuzzbench_repository must be the canonical public repository."
    }

    precondition {
      condition     = local.instance_user_data_gzip_bytes[each.key] <= 16384
      error_message = "Rendered EC2 user data exceeds the 16,384-byte API limit for ${each.key}."
    }

    precondition {
      condition     = local.echidna_ci_input_count == 0 || local.echidna_ci_input_count == length(local.echidna_ci_inputs)
      error_message = "Echidna CI artifact mode requires repo, run ID, artifact name, artifact SHA-256, full commit, and token SSM parameter together."
    }

    precondition {
      condition     = !local.echidna_ci_enabled || can(regex("(?i)linux", var.echidna_ci_artifact_name))
      error_message = "Echidna CI artifact mode requires an artifact name identifying Linux."
    }

    precondition {
      condition     = !local.echidna_ci_enabled || local.echidna_ci_selected
      error_message = "Echidna CI artifact mode requires echidna in the selected fuzzers."
    }

    precondition {
      condition     = var.echidna_ci_token_kms_key_arn == "" || local.echidna_ci_enabled
      error_message = "echidna_ci_token_kms_key_arn is valid only with Echidna CI artifact mode."
    }

    precondition {
      condition     = local.medusa_source_input_count == 0 || local.medusa_source_input_count == length(local.medusa_source_inputs)
      error_message = "Medusa source mode requires git repo, git ref, and full commit together."
    }

    precondition {
      condition     = !local.medusa_source_enabled || local.medusa_source_selected
      error_message = "Medusa source mode requires medusa in the selected fuzzers."
    }
  }

  tags = merge(local.tags, {
    Name     = "${local.name_prefix}-${each.value.fuzzer.key}-${each.value.run_index}"
    Fuzzer   = each.value.fuzzer.key
    RunIndex = tostring(each.value.run_index)
  })
}
