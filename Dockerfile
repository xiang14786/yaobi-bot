FROM python:3.11-slim

# 安裝 swap 所需工具（util-linux 含 mkswap/swapon，procps 含 free）
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start.sh

CMD ["bash", "start.sh"]
