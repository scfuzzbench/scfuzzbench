output "bucket_name" {
  description = "S3 bucket used for logs."
  value       = local.bucket_name
}

output "run_id" {
  description = "Run identifier used in the S3 prefix."
  value       = local.run_id
}

output "run_started_at_epoch" {
  description = "Unix timestamp generated with the run identity before Terraform initialization."
  value       = local.run_started_at_epoch
}

output "terraform_backend_key" {
  description = "Run-scoped remote-state key."
  value       = var.terraform_backend_key
}

output "run_namespace" {
  description = "Run-scoped prefix used for AWS resource names."
  value       = local.name_prefix
}

output "benchmark_uuid" {
  description = "Benchmark UUID used for S3 prefixes."
  value       = nonsensitive(local.benchmark_uuid)
}

output "ssh_private_key_path" {
  description = "Local path to the generated SSH private key."
  value       = local_sensitive_file.ssh_private_key.filename
}

output "instance_ids" {
  description = "Instance IDs by fuzzer and index."
  value       = { for key, instance in aws_instance.fuzzer : key => instance.id }
}

output "instance_public_ips" {
  description = "Public IPs by fuzzer and index."
  value       = { for key, instance in aws_instance.fuzzer : key => instance.public_ip }
}
