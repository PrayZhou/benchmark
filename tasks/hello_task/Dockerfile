FROM python:3.11-slim
RUN useradd --create-home --uid 10001 runner
WORKDIR /workspace
RUN chown -R runner:runner /workspace
USER runner
CMD ["sleep", "infinity"]
