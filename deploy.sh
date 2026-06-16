#!/usr/bin/env bash
set -euo pipefail

command -v jq >/dev/null 2>&1 || { echo "Error: jq is required (brew install jq)" >&2; exit 1; }

# ─── Configuration ────────────────────────────────────────────────────────────
FUNCTION_NAME="ffmpeg-slideshow"
REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_NAME="ffmpeg-slideshow"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
S3_BUCKET="ffmpeg-slideshow-${ACCOUNT_ID}"
ROLE_NAME="ffmpeg-slideshow-role"

echo "=================================================="
echo " FFmpeg Slideshow — Lambda Deploy"
echo "=================================================="
echo " Account : ${ACCOUNT_ID}"
echo " Region  : ${REGION}"
echo " Bucket  : ${S3_BUCKET}"
echo " ECR     : ${ECR_URI}"
echo "=================================================="

# ─── 1. S3 bucket ─────────────────────────────────────────────────────────────
echo ""
echo "[1/5] S3 bucket…"
if aws s3api head-bucket --bucket "${S3_BUCKET}" 2>/dev/null; then
    echo "      Already exists — skipping."
else
    if [ "${REGION}" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}"
    else
        aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}" \
            --create-bucket-configuration LocationConstraint="${REGION}"
    fi
    echo "      Created."
fi

# Block all public access
aws s3api put-public-access-block \
    --bucket "${S3_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --region "${REGION}"

# ─── 2. ECR repository ────────────────────────────────────────────────────────
echo ""
echo "[2/5] ECR repository…"
aws ecr create-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${REGION}" 2>/dev/null && echo "      Created." || echo "      Already exists — skipping."

aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# ─── 3. Docker build & push ───────────────────────────────────────────────────
echo ""
echo "[3/5] Docker build & push…"
docker buildx build \
    --platform linux/amd64 \
    --provenance=false \
    -t "${ECR_URI}:latest" \
    --push \
    .

# ─── 4. IAM role ──────────────────────────────────────────────────────────────
echo ""
echo "[4/5] IAM role…"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

S3_POLICY=$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET}",
        "arn:aws:s3:::${S3_BUCKET}/*"
      ]
    }
  ]
}
POLICY
)

if ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text 2>/dev/null); then
    echo "      Role already exists."
else
    ROLE_ARN=$(aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --query 'Role.Arn' --output text)

    aws iam attach-role-policy \
        --role-name "${ROLE_NAME}" \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

    echo "      Waiting 15 s for IAM propagation…"
    sleep 15
fi

# Always apply S3 inline policy (idempotent — overwrites if already exists)
echo "      Applying S3 policy…"
aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "s3-slideshow-access" \
    --policy-document "${S3_POLICY}"

echo "      Role ARN: ${ROLE_ARN}"

# ─── 5. Lambda function ───────────────────────────────────────────────────────
echo ""
echo "[5/5] Lambda function…"
IMAGE_URI="${ECR_URI}:latest"

# Fetch existing function config (env vars, memory, timeout, ephemeral storage,
# package type — e.g. anything set manually via the console) so the update
# below merges into it instead of wiping it out.
EXISTING_CONFIG=$(aws lambda get-function \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query 'Configuration' \
    --output json 2>/dev/null || echo "null")
if [ "${EXISTING_CONFIG}" = "null" ]; then
    EXISTING_CONFIG="{}"
fi
EXISTING_ENV_JSON=$(echo "${EXISTING_CONFIG}" | jq '.Environment.Variables // {}')
EXISTING_PKG_TYPE=$(echo "${EXISTING_CONFIG}" | jq -r '.PackageType // "NONE"')
EXISTING_TIMEOUT=$(echo "${EXISTING_CONFIG}" | jq -r '.Timeout // empty')
EXISTING_MEMORY=$(echo "${EXISTING_CONFIG}" | jq -r '.MemorySize // empty')
EXISTING_EPHEMERAL=$(echo "${EXISTING_CONFIG}" | jq -r '.EphemeralStorage.Size // empty')

# Build the subset of environment variables this script manages.
# TITLE_TEXT/SUBTITLE_TEXT/LOCATION_TEXT etc. are only overridden when the
# shell env var is explicitly passed in — otherwise the existing (e.g.
# console-set) value is left untouched.
NEW_ENV_JSON=$(jq -n --arg output_bucket "${S3_BUCKET}" '{OUTPUT_BUCKET: $output_bucket}')

if [ -n "${TITLE_TEXT:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${TITLE_TEXT}" '. + {TITLE_TEXT: $v}')
    echo "      Title    : ${TITLE_TEXT}"
else
    echo "      Title    : not passed — keeping existing value, if any"
fi

if [ -n "${SUBTITLE_TEXT:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${SUBTITLE_TEXT}" '. + {SUBTITLE_TEXT: $v}')
    echo "      Subtitle : ${SUBTITLE_TEXT}"
else
    echo "      Subtitle : not passed — keeping existing value, if any"
fi

if [ -n "${LOCATION_TEXT:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${LOCATION_TEXT}" '. + {LOCATION_TEXT: $v}')
    echo "      Location : ${LOCATION_TEXT}"
else
    echo "      Location : not passed — keeping existing value, if any"
fi

if [ -n "${UNSPLASH_ACCESS_KEY:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${UNSPLASH_ACCESS_KEY}" '. + {UNSPLASH_ACCESS_KEY: $v}')
    echo "      Unsplash API key : set"
else
    echo "      Unsplash API key : not set (env var not passed — keeping existing value, if any)"
fi

if [ -n "${MUSIC_S3_KEY:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${MUSIC_S3_KEY}" '. + {MUSIC_S3_KEY: $v}')
    echo "      Music (S3 key)   : ${MUSIC_S3_KEY}"
elif [ -n "${MUSIC_URL:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${MUSIC_URL}" '. + {MUSIC_URL: $v}')
    echo "      Music (URL)      : set"
elif [ -n "${JAMENDO_CLIENT_ID:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${JAMENDO_CLIENT_ID}" '. + {JAMENDO_CLIENT_ID: $v}')
    echo "      Music (Jamendo)  : client_id set"
else
    echo "      Music            : not passed — keeping existing value, if any"
fi

if [ -n "${VIDEOS_S3_BUCKET:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${VIDEOS_S3_BUCKET}" '. + {VIDEOS_S3_BUCKET: $v}')
    echo "      Videos bucket    : ${VIDEOS_S3_BUCKET}"
else
    echo "      Videos bucket    : not passed — keeping existing value, if any"
fi

if [ -n "${VIDEOS_S3_PREFIX:-}" ]; then
    NEW_ENV_JSON=$(echo "${NEW_ENV_JSON}" | jq --arg v "${VIDEOS_S3_PREFIX}" '. + {VIDEOS_S3_PREFIX: $v}')
    echo "      Videos prefix    : ${VIDEOS_S3_PREFIX}"
fi

# Merge: existing console-set values, overridden by anything this script sets
MERGED_ENV_JSON=$(jq -n --argjson existing "${EXISTING_ENV_JSON}" --argjson new "${NEW_ENV_JSON}" '$existing + $new')

# Apply defaults only where a value has never been set (e.g. first-ever deploy)
MERGED_ENV_JSON=$(echo "${MERGED_ENV_JSON}" | jq '
    .TITLE_TEXT    //= "Family Memories" |
    .SUBTITLE_TEXT //= "Summer 2025" |
    .LOCATION_TEXT //= "Japan"
')
ENV_VARS=$(jq -n --argjson vars "${MERGED_ENV_JSON}" '{Variables: $vars}')

# Memory / timeout / ephemeral storage: only override what's explicitly passed
# in via shell env vars (LAMBDA_TIMEOUT / LAMBDA_MEMORY_SIZE /
# LAMBDA_EPHEMERAL_STORAGE_SIZE) — otherwise keep the existing (e.g.
# console-set) value, falling back to a sane default on first-ever deploy.
TIMEOUT="${LAMBDA_TIMEOUT:-${EXISTING_TIMEOUT:-900}}"
MEMORY_SIZE="${LAMBDA_MEMORY_SIZE:-${EXISTING_MEMORY:-3008}}"
EPHEMERAL_SIZE="${LAMBDA_EPHEMERAL_STORAGE_SIZE:-${EXISTING_EPHEMERAL:-512}}"
echo "      Timeout  : ${TIMEOUT}s"
echo "      Memory   : ${MEMORY_SIZE}MB"
echo "      Ephemeral storage : ${EPHEMERAL_SIZE}MB"

if [ "${EXISTING_PKG_TYPE}" = "Image" ]; then
    echo "      Updating code…"
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --image-uri "${IMAGE_URI}" \
        --region "${REGION}" \
        --output text --no-cli-pager

    echo "      Waiting for update to finish…"
    aws lambda wait function-updated \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}"

    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout "${TIMEOUT}" \
        --memory-size "${MEMORY_SIZE}" \
        --ephemeral-storage "Size=${EPHEMERAL_SIZE}" \
        --environment "${ENV_VARS}" \
        --region "${REGION}" \
        --output text --no-cli-pager
elif [ "${EXISTING_PKG_TYPE}" = "Zip" ]; then
    echo "      Existing function is Zip type — deleting to recreate as Image…"
    aws lambda delete-function \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}"
    echo "      Waiting for deletion…"
    sleep 5
    echo "      Creating Image-based function…"
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --package-type Image \
        --code "ImageUri=${IMAGE_URI}" \
        --role "${ROLE_ARN}" \
        --timeout "${TIMEOUT}" \
        --memory-size "${MEMORY_SIZE}" \
        --ephemeral-storage "Size=${EPHEMERAL_SIZE}" \
        --environment "${ENV_VARS}" \
        --region "${REGION}" \
        --output text --no-cli-pager
else
    echo "      Creating function…"
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --package-type Image \
        --code "ImageUri=${IMAGE_URI}" \
        --role "${ROLE_ARN}" \
        --timeout "${TIMEOUT}" \
        --memory-size "${MEMORY_SIZE}" \
        --ephemeral-storage "Size=${EPHEMERAL_SIZE}" \
        --environment "${ENV_VARS}" \
        --region "${REGION}" \
        --output text --no-cli-pager
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Deploy complete!"
echo "=================================================="
echo ""
echo " To invoke the function:"
echo ""
echo "   aws lambda invoke \\"
echo "     --function-name ${FUNCTION_NAME} \\"
echo "     --region ${REGION} \\"
echo "     --log-type Tail \\"
echo "     /tmp/response.json && \\"
echo "   cat /tmp/response.json | python3 -m json.tool"
echo ""
echo " The response contains a presigned S3 URL valid for 1 hour."
echo "=================================================="
