terraform {
  required_version = ">= 1.5.0"
}

variable "template_path" {
  type = string
}

variable "repository_root" {
  type = string
}

variable "malicious_value" {
  type = string
}

variable "fuzzer_env" {
  type    = map(string)
  default = {}
}

locals {
  commit     = "0123456789abcdef0123456789abcdef01234567"
  repository = "https://github.com/scfuzzbench/scfuzzbench"
  file_destinations = {
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
  files = {
    for source, destination in local.file_destinations : source => {
      destination = destination
      executable  = endswith(source, ".sh") || endswith(source, ".py")
      sha256      = filesha256("${var.repository_root}/${source}")
    }
  }
  manifest_json = jsonencode({
    schema_version = 1
    repository     = local.repository
    commit         = local.commit
    files          = local.files
  })
  manifest_b64    = base64encode(local.manifest_json)
  manifest_digest = sha256(local.manifest_json)

  modes = {
    echidna-stable = {
      fuzzer_key               = "echidna"
      echidna_ci_repo          = ""
      echidna_ci_run_id        = ""
      echidna_ci_artifact_name = ""
      echidna_ci_artifact_sha  = ""
      echidna_ci_commit        = ""
      echidna_ci_token         = ""
      medusa_git_repo          = ""
      medusa_git_ref           = ""
      medusa_git_commit        = ""
      medusa_go_version        = ""
      medusa_go_sha            = ""
    }
    echidna-ci = {
      fuzzer_key               = "echidna"
      echidna_ci_repo          = "https://github.com/crytic/echidna"
      echidna_ci_run_id        = "123456789"
      echidna_ci_artifact_name = "echidna-linux-x86_64"
      echidna_ci_artifact_sha  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      echidna_ci_commit        = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      echidna_ci_token         = "/scfuzzbench/echidna-ci-token"
      medusa_git_repo          = ""
      medusa_git_ref           = ""
      medusa_git_commit        = ""
      medusa_go_version        = ""
      medusa_go_sha            = ""
    }
    medusa-stable = {
      fuzzer_key               = "medusa"
      echidna_ci_repo          = ""
      echidna_ci_run_id        = ""
      echidna_ci_artifact_name = ""
      echidna_ci_artifact_sha  = ""
      echidna_ci_commit        = ""
      echidna_ci_token         = ""
      medusa_git_repo          = ""
      medusa_git_ref           = ""
      medusa_git_commit        = ""
      medusa_go_version        = ""
      medusa_go_sha            = ""
    }
    medusa-source = {
      fuzzer_key               = "medusa"
      echidna_ci_repo          = ""
      echidna_ci_run_id        = ""
      echidna_ci_artifact_name = ""
      echidna_ci_artifact_sha  = ""
      echidna_ci_commit        = ""
      echidna_ci_token         = ""
      medusa_git_repo          = "https://github.com/crytic/medusa"
      medusa_git_ref           = "main"
      medusa_git_commit        = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      medusa_go_version        = "1.24.0"
      medusa_go_sha            = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
    foundry = {
      fuzzer_key               = "foundry"
      echidna_ci_repo          = ""
      echidna_ci_run_id        = ""
      echidna_ci_artifact_name = ""
      echidna_ci_artifact_sha  = ""
      echidna_ci_commit        = ""
      echidna_ci_token         = ""
      medusa_git_repo          = ""
      medusa_git_ref           = ""
      medusa_git_commit        = ""
      medusa_go_version        = ""
      medusa_go_sha            = ""
    }
    recon = {
      fuzzer_key               = "recon-fuzzer"
      echidna_ci_repo          = ""
      echidna_ci_run_id        = ""
      echidna_ci_artifact_name = ""
      echidna_ci_artifact_sha  = ""
      echidna_ci_commit        = ""
      echidna_ci_token         = ""
      medusa_git_repo          = ""
      medusa_git_ref           = ""
      medusa_git_commit        = ""
      medusa_go_version        = ""
      medusa_go_sha            = ""
    }
  }

  rendered = {
    for mode_name, mode in local.modes : mode_name => templatefile(
      var.template_path,
      {
        bootstrap_installer_sha256_b64          = base64encode(local.files["infrastructure/bootstrap_bundle.py"].sha256)
        bootstrap_manifest_b64                  = local.manifest_b64
        bootstrap_manifest_sha256_b64           = base64encode(local.manifest_digest)
        scfuzzbench_repository_b64              = base64encode(local.repository)
        scfuzzbench_commit_b64                  = base64encode(local.commit)
        fuzzer_key_b64                          = base64encode(mode.fuzzer_key)
        aws_region_b64                          = base64encode("us-east-1")
        s3_bucket_b64                           = base64encode("test-bucket")
        run_id_b64                              = base64encode("gh-1-1")
        run_started_at_epoch_b64                = base64encode("1800000000")
        benchmark_uuid_b64                      = base64encode("0123456789abcdef")
        benchmark_manifest_b64_b64              = base64encode("e30=")
        timeout_seconds_b64                     = base64encode("14400")
        repo_url_b64                            = base64encode(var.malicious_value)
        repo_commit_b64                         = base64encode(local.commit)
        benchmark_type_b64                      = base64encode("property")
        preliminary_interval_seconds_b64        = base64encode("3600")
        run_index_b64                           = base64encode("0")
        foundry_version_b64                     = base64encode("v1.0.0")
        foundry_git_repo_b64                    = base64encode("https://github.com/foundry-rs/foundry")
        foundry_git_ref_b64                     = base64encode(local.commit)
        echidna_version_b64                     = base64encode("2.3.2")
        echidna_ci_repo_b64                     = base64encode(mode.echidna_ci_repo)
        echidna_ci_run_id_b64                   = base64encode(mode.echidna_ci_run_id)
        echidna_ci_artifact_name_b64            = base64encode(mode.echidna_ci_artifact_name)
        echidna_ci_artifact_sha256_b64          = base64encode(mode.echidna_ci_artifact_sha)
        echidna_ci_commit_b64                   = base64encode(mode.echidna_ci_commit)
        echidna_ci_token_ssm_parameter_name_b64 = base64encode(mode.echidna_ci_token)
        medusa_version_b64                      = base64encode("1.5.1")
        medusa_git_repo_b64                     = base64encode(mode.medusa_git_repo)
        medusa_git_ref_b64                      = base64encode(mode.medusa_git_ref)
        medusa_git_commit_b64                   = base64encode(mode.medusa_git_commit)
        medusa_go_version_b64                   = base64encode(mode.medusa_go_version)
        medusa_go_sha256_b64                    = base64encode(mode.medusa_go_sha)
        recon_version_b64                       = base64encode("0.4.18")
        git_token_ssm_parameter_name_b64        = base64encode("")
        seed_corpus_source_b64                  = base64encode("")
        seed_corpus_provenance_source_b64       = base64encode("")
        fuzzer_env_b64 = {
          for key, value in var.fuzzer_env : key => base64encode(value)
        }
      }
    )
  }
  user_data_base64 = {
    for mode_name, value in local.rendered : mode_name => base64gzip(value)
  }
  gzip_bytes = {
    for mode_name, encoded in local.user_data_base64 :
    mode_name => (length(encoded) * 3 / 4) - length(regexall("=", encoded))
  }
}

output "rendered" {
  value = local.rendered
}

output "user_data_base64" {
  value = local.user_data_base64
}

output "gzip_bytes" {
  value = local.gzip_bytes
}

output "manifest_b64" {
  value = local.manifest_b64
}

output "manifest_sha256" {
  value = local.manifest_digest
}
