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

# Fetch existing environment variables (e.g. ones set manually via the console)
# so the update below merges into them instead of wiping them out.
EXISTING_ENV_JSON=$(aws lambda get-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query 'Environment.Variables' \
    --output json 2>/dev/null || echo "null")
if [ "${EXISTING_ENV_JSON}" = "null" ]; then
    EXISTING_ENV_JSON="{}"
fi

# Build the subset of environment variables this script manages
TITLE_TEXT="${TITLE_TEXT:-Family Memories}"
SUBTITLE_TEXT="${SUBTITLE_TEXT:-Summer 2025}"
LOCATION_TEXT="${LOCATION_TEXT:-Japan}"
NEW_ENV_JSON=$(jq -n \
    --arg output_bucket "${S3_BUCKET}" \
    --arg title "${TITLE_TEXT}" \
    --arg subtitle "${SUBTITLE_TEXT}" \
    --arg location "${LOCATION_TEXT}" \
    '{OUTPUT_BUCKET: $output_bucket, TITLE_TEXT: $title, SUBTITLE_TEXT: $subtitle, LOCATION_TEXT: $location}')
echo "      Title    : ${TITLE_TEXT}"
echo "      Subtitle : ${SUBTITLE_TEXT}"
echo "      Location : ${LOCATION_TEXT}"

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
ENV_VARS=$(jq -n --argjson vars "${MERGED_ENV_JSON}" '{Variables: $vars}')

EXISTING_PKG_TYPE=$(aws lambda get-function \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query 'Configuration.PackageType' \
    --output text 2>/dev/null || echo "NONE")

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
        --timeout 900 \
        --memory-size 3008 \
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
        --timeout 900 \
        --memory-size 3008 \
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
        --timeout 900 \
        --memory-size 3008 \
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
