FROM public.ecr.aws/lambda/python:3.12

# AL2023 minimal image lacks tar/xz; dejavu-sans-fonts is the correct AL2023 package name
RUN dnf install -y tar xz dejavu-sans-fonts && dnf clean all

# Copy fonts to a fixed path using Python glob (avoids need for find/xargs)
RUN mkdir -p /opt/fonts && \
    python3 -c "\
import glob,shutil,sys;\
[shutil.copy(g[0],'/opt/fonts/'+n) \
 for n in ['DejaVuSans.ttf','DejaVuSans-Bold.ttf'] \
 for g in [glob.glob('/usr/share/fonts/**/'+n,recursive=True)] \
 if g or sys.exit('Font not found: '+n)]" && \
    ls /opt/fonts/

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
