locals {
  name_prefix = "scfuzzbench"
  tags        = merge({ Project = "scfuzzbench" }, var.tags)

  timeout_seconds = var.timeout_hours * 3600
  run_id          = var.run_id != "" ? var.run_id : time_static.run.unix

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

  foundry_throughput_patch_ref    = "02c05d970d2801da0aef8b82486ce84b01ede36d"
  foundry_throughput_patch_path   = "${path.module}/../fuzzers/foundry/throughput-progress.patch"
  foundry_throughput_patch_sha256 = filesha256(local.foundry_throughput_patch_path)
  foundry_source_patch = var.foundry_git_repo != "" && var.foundry_git_ref == local.foundry_throughput_patch_ref ? (
    "scfuzzbench-throughput-progress-v1@sha256:${local.foundry_throughput_patch_sha256}"
  ) : ""

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

  benchmark_manifest = merge({
    scfuzzbench_commit           = var.scfuzzbench_commit
    target_repo_url              = var.target_repo_url
    target_commit                = var.target_commit
    benchmark_type               = var.benchmark_type
    instance_type                = var.instance_type
    instances_per_fuzzer         = var.instances_per_fuzzer
    timeout_hours                = var.timeout_hours
    aws_region                   = var.aws_region
    ubuntu_ami_id                = data.aws_ssm_parameter.ubuntu_ami.value
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
    fuzzer_keys                  = sort([for fuzzer in local.fuzzer_definitions : fuzzer.key])
    }, var.shared_seed_corpus_source != "" ? {
    seed_corpus = {
      source         = local.seed_corpus_provenance_source
      source_type    = local.seed_corpus_source_type
      copy_semantics = "recursive-byte-for-byte"
    }
  } : {})

  benchmark_manifest_json = jsonencode(local.benchmark_manifest)
  benchmark_manifest_b64  = base64encode(local.benchmark_manifest_json)
  benchmark_uuid          = md5(local.benchmark_manifest_json)

  default_fuzzer_env = {
    ECHIDNA_CONFIG     = "echidna.yaml"
    ECHIDNA_TARGET     = "test/recon/CryticTester.sol"
    ECHIDNA_CONTRACT   = "CryticTester"
    ECHIDNA_EXTRA_ARGS = "--test-limit 1000000000"
  }
  merged_fuzzer_env = merge(local.default_fuzzer_env, var.fuzzer_env)

  base_fuzzer_definitions = [
    {
      key          = "echidna"
      install_path = "${path.module}/../fuzzers/echidna/install.sh"
      run_path     = "${path.module}/../fuzzers/echidna/run.sh"
    },
    {
      key          = "medusa"
      install_path = "${path.module}/../fuzzers/medusa/install.sh"
      run_path     = "${path.module}/../fuzzers/medusa/run.sh"
    },
    {
      key          = "foundry"
      install_path = "${path.module}/../fuzzers/foundry/install.sh"
      run_path     = "${path.module}/../fuzzers/foundry/run.sh"
    },
    {
      key          = "recon-fuzzer"
      install_path = "${path.module}/../fuzzers/recon-fuzzer/install.sh"
      run_path     = "${path.module}/../fuzzers/recon-fuzzer/run.sh"
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
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "time_static" "run" {}

data "aws_ssm_parameter" "ubuntu_ami" {
  name = var.ubuntu_ami_ssm_parameter
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
  count = local.bucket_name != "" ? 1 : 0

  bucket                  = local.bucket_name
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
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [
      "arn:aws:s3:::${local.bucket_name}",
      "arn:aws:s3:::${local.bucket_name}/*",
    ]
  }

  statement {
    sid     = "VerifyImmutableBenchmarkManifests"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/logs/*/manifest.json",
      "arn:aws:s3:::${local.bucket_name}/runs/*/manifest.json",
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
  count = var.bucket_public_read ? 1 : 0

  bucket = local.bucket_name
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
}

resource "aws_instance" "fuzzer" {
  for_each = local.instance_map

  ami                         = data.aws_ssm_parameter.ubuntu_ami.value
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

  user_data_base64 = base64gzip(templatefile("${path.module}/user_data.sh.tftpl", {
    fuzzer_key                          = each.value.fuzzer.key
    shared_sh                           = file("${path.module}/../fuzzers/_shared/common.sh")
    seed_corpus_helper                  = file("${path.module}/../fuzzers/_shared/prepare_seed_corpus.py")
    install_sh                          = file(each.value.fuzzer.install_path)
    run_sh                              = file(each.value.fuzzer.run_path)
    echidna_ci_enabled                  = each.value.fuzzer.key == "echidna" && local.echidna_ci_enabled
    echidna_ci_extractor                = each.value.fuzzer.key == "echidna" && local.echidna_ci_enabled ? file("${path.module}/../fuzzers/echidna/extract_ci_artifact.py") : ""
    medusa_source_enabled               = each.value.fuzzer.key == "medusa" && local.medusa_source_enabled
    medusa_go_extractor                 = each.value.fuzzer.key == "medusa" && local.medusa_source_enabled ? file("${path.module}/../fuzzers/medusa/extract_go_toolchain.py") : ""
    aws_region                          = var.aws_region
    s3_bucket                           = local.bucket_name
    run_id                              = local.run_id
    benchmark_uuid                      = local.benchmark_uuid
    benchmark_manifest_b64              = local.benchmark_manifest_b64
    timeout_seconds                     = local.timeout_seconds
    repo_url                            = var.target_repo_url
    repo_commit                         = var.target_commit
    benchmark_type                      = var.benchmark_type
    foundry_version                     = local.foundry_release_version
    foundry_git_repo                    = var.foundry_git_repo
    foundry_git_ref                     = var.foundry_git_ref
    foundry_source_patch                = file(local.foundry_throughput_patch_path)
    echidna_version                     = var.echidna_version
    echidna_ci_repo                     = each.value.fuzzer.key == "echidna" ? var.echidna_ci_repo : ""
    echidna_ci_run_id                   = each.value.fuzzer.key == "echidna" ? var.echidna_ci_run_id : ""
    echidna_ci_artifact_name            = each.value.fuzzer.key == "echidna" ? var.echidna_ci_artifact_name : ""
    echidna_ci_artifact_sha256          = each.value.fuzzer.key == "echidna" ? var.echidna_ci_artifact_sha256 : ""
    echidna_ci_commit                   = each.value.fuzzer.key == "echidna" ? var.echidna_ci_commit : ""
    echidna_ci_token_ssm_parameter_name = each.value.fuzzer.key == "echidna" ? var.echidna_ci_token_ssm_parameter_name : ""
    medusa_version                      = var.medusa_version
    medusa_git_repo                     = each.value.fuzzer.key == "medusa" ? var.medusa_git_repo : ""
    medusa_git_ref                      = each.value.fuzzer.key == "medusa" ? var.medusa_git_ref : ""
    medusa_git_commit                   = each.value.fuzzer.key == "medusa" ? var.medusa_git_commit : ""
    medusa_go_version                   = each.value.fuzzer.key == "medusa" && local.medusa_source_enabled ? var.medusa_go_version : ""
    medusa_go_sha256                    = each.value.fuzzer.key == "medusa" && local.medusa_source_enabled ? var.medusa_go_sha256 : ""
    recon_version                       = var.recon_version
    git_token_ssm_parameter_name        = var.git_token_ssm_parameter_name
    seed_corpus_source                  = var.shared_seed_corpus_source
    seed_corpus_provenance_source       = local.seed_corpus_provenance_source
    fuzzer_env                          = local.merged_fuzzer_env
  }))

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required"
  }

  lifecycle {
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
