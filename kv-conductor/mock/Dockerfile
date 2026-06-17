FROM python:3.11-slim

RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    pyzmq msgpack requests xxhash

COPY mock/zmq_publisher.py /usr/local/bin/zmq_publisher.py

ENTRYPOINT ["python3", "/usr/local/bin/zmq_publisher.py"]
