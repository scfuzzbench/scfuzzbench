locals {
  name_prefix = "scfuzzbench"
  tags        = merge({ Project = "scfuzzbench" }, var.tags)

  timeout_seconds = var.timeout_hours * 3600
  run_id          = var.run_id != "" ? var.run_id : time_static.run.unix
  run_started_at_epoch = (
    var.run_started_at_epoch != "" ? var.run_started_at_epoch :
    can(regex("^[0-9]+$", local.run_id)) ? local.run_id :
    time_static.run.unix
  )

  # Pick an AZ that supports the requested instance type to avoid flaky applies
  # when AWS auto-selects an AZ where the type isn't offered.
  subnet_availability_zone = var.availability_zone != "" ? var.availability_zone : sort(data.aws_ec2_instance_type_offerings.fuzzer.locations)[0]

  benchmark_manifest = {
    scfuzzbench_commit           = var.scfuzzbench_commit
    target_repo_url              = var.target_repo_url
    target_commit                = var.target_commit
    benchmark_type               = var.benchmark_type
    instance_type                = var.instance_type
    instances_per_fuzzer         = var.instances_per_fuzzer
    timeout_hours                = var.timeout_hours
    preliminary_interval_seconds = var.preliminary_interval_seconds
    aws_region                   = var.aws_region
    ubuntu_ami_id                = data.aws_ssm_parameter.ubuntu_ami.value
    foundry_version              = var.foundry_version
    foundry_git_repo             = var.foundry_git_repo
    foundry_git_ref              = var.foundry_git_ref
    echidna_version              = var.echidna_version
    medusa_version               = var.medusa_version
    recon_version                = var.recon_version
    fuzzer_keys                  = sort([for fuzzer in local.fuzzer_definitions : fuzzer.key])
  }

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
  bucket_name                 = var.existing_bucket_name != "" ? var.existing_bucket_name : try(aws_s3_bucket.logs[0].bucket, "")
  git_token_ssm_parameter_arn = var.git_token_ssm_parameter_name != "" ? "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.git_token_ssm_parameter_name, "/")}" : ""
  ssm_parameter_arns          = local.git_token_ssm_parameter_arn != "" ? [local.git_token_ssm_parameter_arn] : []
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

  dynamic "statement" {
    for_each = local.ssm_parameter_arns

    content {
      actions   = ["ssm:GetParameter"]
      resources = [statement.value]
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

resource "aws_instance" "fuzzer" {
  for_each = local.instance_map

  ami                                  = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type                        = var.instance_type
  associate_public_ip_address          = true
  subnet_id                            = aws_subnet.public.id
  vpc_security_group_ids               = [aws_security_group.ssh.id]
  key_name                             = aws_key_pair.ssh.key_name
  iam_instance_profile                 = aws_iam_instance_profile.fuzzer.name
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true

  user_data_base64 = base64gzip(templatefile("${path.module}/user_data.sh.tftpl", {
    fuzzer_key                   = each.value.fuzzer.key
    shared_sh                    = file("${path.module}/../fuzzers/_shared/common.sh")
    install_sh                   = file(each.value.fuzzer.install_path)
    run_sh                       = file(each.value.fuzzer.run_path)
    preliminary_snapshot_py      = file("${path.module}/../scripts/preliminary_snapshot.py")
    aws_region                   = var.aws_region
    s3_bucket                    = local.bucket_name
    run_id                       = local.run_id
    run_started_at_epoch         = local.run_started_at_epoch
    benchmark_uuid               = local.benchmark_uuid
    benchmark_manifest_b64       = local.benchmark_manifest_b64
    timeout_seconds              = local.timeout_seconds
    repo_url                     = var.target_repo_url
    repo_commit                  = var.target_commit
    benchmark_type               = var.benchmark_type
    preliminary_interval_seconds = var.preliminary_interval_seconds
    run_index                    = each.value.run_index
    foundry_version              = var.foundry_version
    foundry_git_repo             = var.foundry_git_repo
    foundry_git_ref              = var.foundry_git_ref
    echidna_version              = var.echidna_version
    medusa_version               = var.medusa_version
    recon_version                = var.recon_version
    git_token_ssm_parameter_name = var.git_token_ssm_parameter_name
    fuzzer_env                   = local.merged_fuzzer_env
  }))

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required"
  }

  tags = merge(local.tags, {
    Name     = "${local.name_prefix}-${each.value.fuzzer.key}-${each.value.run_index}"
    Fuzzer   = each.value.fuzzer.key
    RunIndex = tostring(each.value.run_index)
  })
}
