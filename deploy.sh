#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
FUNCTION_NAME="ffmpeg-slideshow"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
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
docker build --platform linux/amd64 --provenance=false -t "${ECR_REPO_NAME}:latest" .
docker tag "${ECR_REPO_NAME}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

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

# Build Lambda environment variables
TITLE_TEXT="${TITLE_TEXT:-Family Memories}"
SUBTITLE_TEXT="${SUBTITLE_TEXT:-Summer 2025}"
LOCATION_TEXT="${LOCATION_TEXT:-Japan}"
ENV_VARS="OUTPUT_BUCKET=${S3_BUCKET},TITLE_TEXT=${TITLE_TEXT},SUBTITLE_TEXT=${SUBTITLE_TEXT},LOCATION_TEXT=${LOCATION_TEXT}"
echo "      Title    : ${TITLE_TEXT}"
echo "      Subtitle : ${SUBTITLE_TEXT}"
echo "      Location : ${LOCATION_TEXT}"

if [ -n "${UNSPLASH_ACCESS_KEY:-}" ]; then
    ENV_VARS="${ENV_VARS},UNSPLASH_ACCESS_KEY=${UNSPLASH_ACCESS_KEY}"
    echo "      Unsplash API key : set"
else
    echo "      Unsplash API key : not set (gradient fallback)"
fi

if [ -n "${MUSIC_S3_KEY:-}" ]; then
    ENV_VARS="${ENV_VARS},MUSIC_S3_KEY=${MUSIC_S3_KEY}"
    echo "      Music (S3 key)   : ${MUSIC_S3_KEY}"
elif [ -n "${MUSIC_URL:-}" ]; then
    ENV_VARS="${ENV_VARS},MUSIC_URL=${MUSIC_URL}"
    echo "      Music (URL)      : set"
elif [ -n "${JAMENDO_CLIENT_ID:-}" ]; then
    ENV_VARS="${ENV_VARS},JAMENDO_CLIENT_ID=${JAMENDO_CLIENT_ID}"
    echo "      Music (Jamendo)  : client_id set"
else
    echo "      Music            : not set (no BGM)"
fi

ENV_VARS="Variables={${ENV_VARS}}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" &>/dev/null; then
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
