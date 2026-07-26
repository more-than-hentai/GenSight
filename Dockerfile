FROM python:3.12-slim

# Build with --build-arg WITH_ML=true to include the WD Tagger extras
# (auto-tagging + content ratings). Off by default: the CUDA runtime
# pulled in by requirements-ml.txt adds roughly 2 GB to the image.
ARG WITH_ML=false

WORKDIR /opt/gensight

COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_ML" = "true" ]; then \
      pip install --no-cache-dir -r requirements-ml.txt; \
    fi

COPY app ./app
COPY web ./web

ENV GENSIGHT_DATA_DIR=/opt/gensight/data
# onnxruntime-gpu resolves libcudart/libcudnn through the loader and the
# pip CUDA runtime lives under site-packages, so it has to be on the
# library path. Harmless when the ML extras are not installed.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
# Cache the tagger model in the data volume instead of re-downloading
# it on every container start.
ENV HF_HOME=/opt/gensight/data/hf
VOLUME ["/opt/gensight/data"]

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
