FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVER_DATA_DIR=/data
WORKDIR /app
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir --only-binary=imageio-ffmpeg --timeout 30 --retries 2 -r requirements-runtime.txt
RUN python -c "import imageio_ffmpeg, subprocess; subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True, stdout=subprocess.DEVNULL)"
COPY --chmod=644 archiver.py ./
USER 1000:1000
ENTRYPOINT ["python", "archiver.py"]
CMD ["run-user"]
