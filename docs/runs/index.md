# Runs

This page is generated in CI from the S3 run index (`runs/<run_id>/<benchmark_uuid>/manifest.json`).

::: tip
Only **complete** runs are shown (timeout + 1h grace).

See [Active preliminary results](/preliminary/) for explicitly incomplete,
non-terminal checkpoints from campaigns that are still running.

If you are previewing locally, run the generator first:

```bash
python3 scripts/generate_docs_site.py --bucket "$SCFUZZBENCH_BUCKET" --region "$AWS_REGION"
```
:::
