FROM node:22-slim
RUN npm install -g @anthropic-ai/claude-code && \
    apt-get update && apt-get install -y screen python3 python3-pip git && \
    pip3 install pyyaml --break-system-packages && \
    rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent
ENTRYPOINT ["claude"]
