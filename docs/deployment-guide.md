# Deployment guide: DocFlow on AWS

This guide takes an empty AWS account to a running, HTTPS-served DocFlow with automatic deploys
from GitHub. Follow it in order. Every command runs in a Linux shell (on Windows: WSL).

**Result:** one EC2 instance in `ap-south-1` (Mumbai) running the whole stack with Docker
Compose behind Caddy (HTTPS from Let's Encrypt), files and backups in S3, secrets in SSM Parameter
Store, webhook signing secrets encrypted with KMS, logs in CloudWatch, and deploys from GitHub
Actions without stored AWS keys.

**Cost:** about USD 25/month (t3.small, public IPv4, 20 GB disk, KMS key, small S3/ECR/CloudWatch
usage). Confirm in the AWS Pricing Calculator. `terraform destroy` removes everything (step 10).

---

## 1. Prepare the AWS account

1. **Protect the root user with MFA:** console, account name (top right), *Security credentials*,
   *Assign MFA device*. Do not create access keys for the root user.
2. **Plan:** new accounts can start on the *Free plan*, which only offers selected services and
   closes the account after six months. Upgrade to the *Paid plan* (Billing and Cost Management,
   *Upgrade plan*). Sign-up credits carry over.
3. **Budget alert:** Billing and Cost Management, *Budgets*, *Create budget*, monthly cost budget,
   for example USD 10, with your email.
4. **A personal admin user for the CLI** (IAM Identity Center), in region `ap-south-1`:
   enable IAM Identity Center; add a user (set the password and MFA from the invitation email);
   create a permission set from the predefined *AdministratorAccess* policy; assign the user and
   permission set to the account; copy the *AWS access portal URL*.

## 2. Install the tools

- AWS CLI v2 and Terraform >= 1.17 (any install method; for example user-local binaries in
  `~/.local/bin`), Python 3, and optionally the GitHub CLI `gh`.

Configure the CLI with the Identity Center user (temporary credentials, no access keys):

```bash
aws configure sso
#   SSO session name: docflow      SSO start URL: <access portal URL>
#   SSO region: ap-south-1         scopes: (default)
#   then choose the account and AdministratorAccess; default region ap-south-1; profile: docflow
aws sts get-caller-identity --profile docflow     # shows AWSReservedSSO_AdministratorAccess
```

Sessions expire after some hours; renew with `aws sso login --profile docflow`.

## 3. Find your repository's OIDC subject prefix

GitHub Actions authenticates to AWS with an OIDC token. Repositories using GitHub's *immutable
subjects* put the owner and repository ids in the token's subject, and the AWS role must match it
exactly:

```bash
gh api repos/OWNER/REPO/actions/oidc/customization/sub
#   {"use_default":true,"use_immutable_subject":true,
#    "sub_claim_prefix":"repo:OWNER@123/REPO@456"}
```

Use `sub_claim_prefix` below. Without immutable subjects the prefix is `repo:OWNER/REPO`.

## 4. Create the infrastructure

```bash
cd infra
terraform init
terraform plan -out=docflow.tfplan \
  -var 'github_oidc_subject_prefix=<sub_claim_prefix from step 3>' \
  -var 'aws_profile=docflow'
# Read the plan: on a fresh account it creates 28 resources and changes or destroys nothing.
terraform apply docflow.tfplan
terraform output          # url, hostname, instance_id, ecr_repository_url, bucket, deploy_role_arn
```

The server boots and installs Docker and Compose by itself (a few minutes). Check it is reachable
through SSM (there is no SSH):

```bash
aws ssm describe-instance-information --profile docflow \
  --query "InstanceInformationList[].{id:InstanceId,ping:PingStatus}" --output table
```

**A second environment** (staging, or a drill) in the same account goes in its own Terraform
workspace and, simplest, its own region. The workspace name must equal the environment name (a
guard refuses mismatches, so one environment's variables can never be applied to another's
state; the default workspace holds `prod`), and the account's GitHub OIDC provider already exists:

```bash
terraform workspace new staging
terraform plan -out=docflow.tfplan -var 'github_oidc_subject_prefix=<prefix>' \
  -var environment=staging -var region=ap-southeast-1 \
  -var create_github_oidc_provider=false -var github_environment=staging
```

Use the same `-var` values for every later `plan` and `destroy` of that environment, and its
prefix `/docflow/staging` and region (`export AWS_REGION=ap-southeast-1`) in the commands below.

The hostname is a free `<ip-with-dashes>.sslip.io` name that resolves to the server's Elastic IP;
Caddy obtains a Let's Encrypt certificate for it on first start. To use your own domain, point an
A record at the Elastic IP and change the `site-address` parameter (step 5 table).

## 5. Store the secrets

Secret values never go through Terraform (its state file would hold them in plain text).

```bash
PREFIX=/docflow/prod          # /docflow/<environment>
aws ssm put-parameter --profile docflow --type SecureString --name "$PREFIX/db-password" \
  --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
aws ssm put-parameter --profile docflow --type SecureString --name "$PREFIX/jwt-secret" \
  --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
read -rs -p "OpenAI API key: " OPENAI_KEY; echo
aws ssm put-parameter --profile docflow --type SecureString --name "$PREFIX/openai-api-key" \
  --value "$OPENAI_KEY"; unset OPENAI_KEY
```

| Parameter | Type | Set by |
|---|---|---|
| `db-password`, `jwt-secret`, `openai-api-key` | SecureString | you (above) |
| `s3-bucket`, `kms-key-id`, `site-address` | String | Terraform |
| `admin-password` | SecureString | `deploy/create-admin.sh` (step 8) |

Use an OpenAI key from a dedicated project with a spend limit.

## 6. Connect GitHub

1. **Environment:** repository *Settings*, *Environments*, create `production`; under
   *Deployment branches and tags* allow only `main`. The AWS role trusts this environment, and
   the environment only accepts `main`, so only `main` can deploy.

   ```bash
   gh api --method PUT repos/OWNER/REPO/environments/production \
     --input - <<< '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
   gh api --method POST repos/OWNER/REPO/environments/production/deployment-branch-policies \
     -f name=main -f type=branch
   ```

2. **Repository variables** (not secrets; nothing here is sensitive):

   ```bash
   cd infra
   gh variable set AWS_REGION --body ap-south-1
   gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform output -raw deploy_role_arn)"
   gh variable set DOCFLOW_ECR_REPOSITORY --body "$(terraform output -raw ecr_repository_url)"
   gh variable set DOCFLOW_BUCKET --body "$(terraform output -raw bucket)"
   gh variable set DOCFLOW_INSTANCE_ID --body "$(terraform output -raw instance_id)"
   gh variable set DOCFLOW_HOSTNAME --body "$(terraform output -raw hostname)"
   ```

The deploy job is skipped until `AWS_DEPLOY_ROLE_ARN` is set. The workflow deploys one
environment (`production`); other environments are deployed with `deploy/manual-deploy.sh`.

## 7. Deploy

Push to `main` (or re-run the latest workflow). The pipeline runs the tests, the Docker smoke and
hardening checks, then the `deploy` job:

1. assumes the deploy role with the OIDC token;
2. builds the image and pushes it to ECR, tagged with the commit;
3. uploads `deploy/` to `s3://<bucket>/deploy/<commit>/`;
4. tells the server through SSM Run Command to render secrets (`render-env.sh`), pull, migrate and
   restart (`remote-deploy.sh`), waiting until every service is healthy;
5. checks `https://<hostname>/ready` from the internet.

**Without GitHub** (an environment GitHub does not deploy, or GitHub Actions is down), with Docker
installed: `deploy/manual-deploy.sh` does the same steps from your machine for the commit you
have checked out, using the selected Terraform workspace's outputs.

## 8. Create the first admin

```bash
PREFIX=$(terraform -chdir=infra output -raw ssm_prefix)
deploy/create-admin.sh "$(terraform -chdir=infra output -raw instance_id)" admin@example.com "$PREFIX"
aws ssm get-parameter --profile docflow --with-decryption \
  --name "$PREFIX/admin-password" --query Parameter.Value --output text
```

The password is generated into SSM and read there by the server; it never appears in the
terminal output of the script or in SSM command history. Log in at `https://<hostname>/docs`
(*Authorize*, the email as username), then create personal users with `POST /users`.

## 9. Verify

```bash
H=$(terraform -chdir=infra output -raw hostname)
curl -fsS https://$H/health && curl -fsS https://$H/ready
curl -sS -D - -o /dev/null https://$H/health | grep -iE "strict-transport|x-request-id"
echo | openssl s_client -connect $H:443 -servername $H 2>/dev/null | openssl x509 -noout -issuer
```

Expect `"environment":"production"`, `"database":"ok"`, HSTS and a request id, and a Let's
Encrypt issuer. Logs appear in CloudWatch under `/docflow`, one stream per container.

## 10. Tear down

`terraform destroy` refuses to delete a non-empty S3 bucket or an ECR repository with images
(deliberately: they hold documents and backups). To remove everything:

Read the outputs first; after the destroy they are gone. For an environment in its own
workspace, finish with `terraform -chdir=infra workspace select default` and
`terraform -chdir=infra workspace delete <name>`.

```bash
export AWS_REGION=$(terraform -chdir=infra output -raw region)
BUCKET=$(terraform -chdir=infra output -raw bucket)
PREFIX=$(terraform -chdir=infra output -raw ssm_prefix)
aws s3 rm "s3://$BUCKET" --recursive --profile docflow
# Several passes: deleting a multi-platform image index leaves its child manifests behind.
for _ in 1 2 3; do
  IMAGES=$(aws ecr list-images --profile docflow --repository-name docflow --query imageIds --output json)
  [ "$IMAGES" = "[]" ] && break
  aws ecr batch-delete-image --profile docflow --repository-name docflow --image-ids "$IMAGES" > /dev/null
done
terraform -chdir=infra plan -destroy -out=destroy.tfplan \
  -var 'github_oidc_subject_prefix=<prefix>'    # plus the environment's other -var values
# Read the plan: it must list only this environment's resources.
terraform -chdir=infra apply destroy.tfplan
aws ssm delete-parameters --profile docflow --names \
  "$PREFIX/db-password" "$PREFIX/jwt-secret" "$PREFIX/openai-api-key" "$PREFIX/admin-password"
```

The KMS key is scheduled for deletion after 7 days (it can be cancelled until then).
