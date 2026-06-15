#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
FUNCTION_NAME="ffmpeg-slideshow"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="ffmpeg-slideshow-${ACCOUNT_ID}"
ROLE_NAME="ffmpeg-slideshow-role"
PYTHON_LAYER_NAME="ffmpeg-slideshow-deps"
FFMPEG_LAYER_NAME="ffmpeg-slideshow-ffmpeg"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

echo "=================================================="
echo " FFmpeg Slideshow — Lambda Layer Deploy"
echo "=================================================="
echo " Account : ${ACCOUNT_ID}"
echo " Region  : ${REGION}"
echo " Bucket  : ${S3_BUCKET}"
echo "=================================================="

# ─── 1. S3 bucket ─────────────────────────────────────────────────────────────
echo ""
echo "[1/6] S3 bucket…"
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

aws s3api put-public-access-block \
    --bucket "${S3_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --region "${REGION}"

# ─── 2. IAM role ──────────────────────────────────────────────────────────────
echo ""
echo "[2/6] IAM role…"

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

# Always apply S3 inline policy (idempotent)
aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "s3-slideshow-access" \
    --policy-document "${S3_POLICY}"

echo "      Role ARN: ${ROLE_ARN}"

# ─── 3. Python deps layer (numpy + Pillow) ────────────────────────────────────
echo ""
echo "[3/6] Python dependencies layer…"
PYTHON_BUILD="${BUILD_DIR}/python-layer"
mkdir -p "${PYTHON_BUILD}/python"

# Download manylinux wheels compatible with Lambda's AL2023 runtime (no Docker needed)
python3 -m pip install \
    --platform manylinux2014_x86_64 \
    --python-version 3.12 \
    --implementation cp \
    --abi cp312 \
    --only-binary=:all: \
    --target "${PYTHON_BUILD}/python" \
    Pillow

(cd "${PYTHON_BUILD}" && zip -qr "${BUILD_DIR}/python-deps-layer.zip" python/)
echo "      $(du -sh "${BUILD_DIR}/python-deps-layer.zip" | cut -f1) — python-deps-layer.zip"

aws s3 cp "${BUILD_DIR}/python-deps-layer.zip" "s3://${S3_BUCKET}/layers/python-deps-layer.zip" --region "${REGION}"
PYTHON_LAYER_ARN=$(aws lambda publish-layer-version \
    --layer-name "${PYTHON_LAYER_NAME}" \
    --description "Pillow for ffmpeg-slideshow" \
    --content "S3Bucket=${S3_BUCKET},S3Key=layers/python-deps-layer.zip" \
    --compatible-runtimes python3.12 \
    --compatible-architectures x86_64 \
    --region "${REGION}" \
    --query 'LayerVersionArn' --output text)
echo "      Published: ${PYTHON_LAYER_ARN}"

# ─── 4. FFmpeg + fonts layer ──────────────────────────────────────────────────
echo ""
echo "[4/6] FFmpeg + fonts layer…"
FFMPEG_BUILD="${BUILD_DIR}/ffmpeg-layer"
mkdir -p "${FFMPEG_BUILD}/bin" "${FFMPEG_BUILD}/fonts"

echo "      Downloading FFmpeg static build (linux/amd64)…"
FFMPEG_TMP="${BUILD_DIR}/ffmpeg-tmp"
mkdir -p "${FFMPEG_TMP}"
curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
    | tar -xJ -C "${FFMPEG_TMP}" --strip-components=1
cp "${FFMPEG_TMP}/ffmpeg"  "${FFMPEG_BUILD}/bin/"
cp "${FFMPEG_TMP}/ffprobe" "${FFMPEG_BUILD}/bin/"
chmod +x "${FFMPEG_BUILD}/bin/ffmpeg" "${FFMPEG_BUILD}/bin/ffprobe"

echo "      Downloading DejaVu fonts…"
FONTS_TMP="${BUILD_DIR}/fonts-tmp"
mkdir -p "${FONTS_TMP}"
curl -fsSL \
    "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2" \
    | tar -xj -C "${FONTS_TMP}" --strip-components=2
cp "${FONTS_TMP}/DejaVuSans.ttf"      "${FFMPEG_BUILD}/fonts/"
cp "${FONTS_TMP}/DejaVuSans-Bold.ttf" "${FFMPEG_BUILD}/fonts/"

(cd "${FFMPEG_BUILD}" && zip -qr "${BUILD_DIR}/ffmpeg-layer.zip" bin/ fonts/)
echo "      $(du -sh "${BUILD_DIR}/ffmpeg-layer.zip" | cut -f1) — ffmpeg-layer.zip"

aws s3 cp "${BUILD_DIR}/ffmpeg-layer.zip" "s3://${S3_BUCKET}/layers/ffmpeg-layer.zip" --region "${REGION}"
FFMPEG_LAYER_ARN=$(aws lambda publish-layer-version \
    --layer-name "${FFMPEG_LAYER_NAME}" \
    --description "FFmpeg static binary + DejaVu fonts" \
    --content "S3Bucket=${S3_BUCKET},S3Key=layers/ffmpeg-layer.zip" \
    --compatible-runtimes python3.12 \
    --compatible-architectures x86_64 \
    --region "${REGION}" \
    --query 'LayerVersionArn' --output text)
echo "      Published: ${FFMPEG_LAYER_ARN}"

# ─── 5. Function package ──────────────────────────────────────────────────────
echo ""
echo "[5/6] Function package…"
zip -j "${BUILD_DIR}/function.zip" lambda_function.py
echo "      $(du -sh "${BUILD_DIR}/function.zip" | cut -f1) — function.zip"

# ─── 6. Lambda function ───────────────────────────────────────────────────────
echo ""
echo "[6/6] Lambda function…"

TITLE_TEXT="${TITLE_TEXT:-Family Memories}"
SUBTITLE_TEXT="${SUBTITLE_TEXT:-Summer 2025}"
LOCATION_TEXT="${LOCATION_TEXT:-Japan}"
ENV_VARS="OUTPUT_BUCKET=${S3_BUCKET},TITLE_TEXT=${TITLE_TEXT},SUBTITLE_TEXT=${SUBTITLE_TEXT},LOCATION_TEXT=${LOCATION_TEXT}"
echo "      Title    : ${TITLE_TEXT}"
echo "      Subtitle : ${SUBTITLE_TEXT}"
echo "      Location : ${LOCATION_TEXT}"

# Photos (S3)
if [ -n "${PHOTOS_S3_PREFIX:-}" ]; then
    ENV_VARS="${ENV_VARS},PHOTOS_S3_PREFIX=${PHOTOS_S3_PREFIX}"
    echo "      Photos prefix    : ${PHOTOS_S3_PREFIX}"
    if [ -n "${PHOTOS_S3_BUCKET:-}" ]; then
        ENV_VARS="${ENV_VARS},PHOTOS_S3_BUCKET=${PHOTOS_S3_BUCKET}"
        echo "      Photos bucket    : ${PHOTOS_S3_BUCKET}"
    else
        echo "      Photos bucket    : ${S3_BUCKET} (OUTPUT_BUCKET)"
    fi
else
    echo "      Photos prefix    : not set — set PHOTOS_S3_PREFIX before invoking"
fi

# Music (S3)
if [ -n "${MUSIC_S3_KEY:-}" ]; then
    ENV_VARS="${ENV_VARS},MUSIC_S3_KEY=${MUSIC_S3_KEY}"
    echo "      Music key        : ${MUSIC_S3_KEY}"
    if [ -n "${MUSIC_S3_BUCKET:-}" ]; then
        ENV_VARS="${ENV_VARS},MUSIC_S3_BUCKET=${MUSIC_S3_BUCKET}"
        echo "      Music bucket     : ${MUSIC_S3_BUCKET}"
    else
        echo "      Music bucket     : ${S3_BUCKET} (OUTPUT_BUCKET)"
    fi
else
    echo "      Music key        : not set — no BGM"
fi

ENV_VARS="Variables={${ENV_VARS}}"

# If the function exists as a container image, delete it first (package type cannot be changed in-place)
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" &>/dev/null; then
    EXISTING_TYPE=$(aws lambda get-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}" \
        --query 'PackageType' --output text)
    if [ "${EXISTING_TYPE}" = "Image" ]; then
        echo "      Existing function is container Image type — deleting to recreate as Zip…"
        aws lambda delete-function \
            --function-name "${FUNCTION_NAME}" \
            --region "${REGION}"
        FUNCTION_EXISTS=false
    else
        FUNCTION_EXISTS=true
    fi
else
    FUNCTION_EXISTS=false
fi

if [ "${FUNCTION_EXISTS}" = "true" ]; then
    echo "      Updating code…"
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${BUILD_DIR}/function.zip" \
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
        --layers "${FFMPEG_LAYER_ARN}" "${PYTHON_LAYER_ARN}" \
        --region "${REGION}" \
        --output text --no-cli-pager
else
    echo "      Creating function…"
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --zip-file "fileb://${BUILD_DIR}/function.zip" \
        --role "${ROLE_ARN}" \
        --timeout 900 \
        --memory-size 3008 \
        --environment "${ENV_VARS}" \
        --layers "${FFMPEG_LAYER_ARN}" "${PYTHON_LAYER_ARN}" \
        --architectures x86_64 \
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
