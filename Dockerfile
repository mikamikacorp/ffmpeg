FROM public.ecr.aws/lambda/python:3.12

# AL2023 minimal image lacks tar/xz
RUN dnf install -y tar xz && dnf clean all

# Noto Sans CJK JP covers Latin + Japanese (hiragana/katakana/kanji) + box-drawing
# glyphs, so テロップ (on-screen text) renders correctly whether TITLE_TEXT etc.
# are set in English or Japanese.
RUN mkdir -p /opt/fonts && \
    curl -fsSL -o /opt/fonts/NotoSansCJKjp-Regular.otf \
        "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf" && \
    curl -fsSL -o /opt/fonts/NotoSansCJKjp-Bold.otf \
        "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJKjp-Bold.otf" && \
    ls -la /opt/fonts/

# Install static FFmpeg build — glob expansion avoids find/xargs
RUN curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
        -o /tmp/ffmpeg.tar.xz && \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp && \
    cp /tmp/ffmpeg*/ffmpeg  /usr/local/bin/ffmpeg && \
    cp /tmp/ffmpeg*/ffprobe /usr/local/bin/ffprobe && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf /tmp/ffmpeg* && \
    ffmpeg -version 2>&1 | head -1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lambda_function.py ${LAMBDA_TASK_ROOT}

CMD ["lambda_function.lambda_handler"]
