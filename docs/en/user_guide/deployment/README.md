# Deployment

MindIE Motor supports the following two deployment methods, both of which are best practices and can be selected based on your environment.

## K8s Deployment

This method applies to scenarios where a Kubernetes cluster already exists. Resource files are generated and applied with one click through the deployer tool, supporting multiple deployment forms such as PD disaggregation and PD co-location, and providing complete service discovery, load balancing, and self-healing capabilities.

→ Start from [Deployment Mode Description](k8s/README.md)

## Docker Deployment

It is suitable for scenarios on a single machine or without a K8s environment. The inference service can be started with only a Docker container and host-mounted configurations, making it lightweight and fast.

→ See [Single-Container Deployment](docker/single_container.md) or [Multi-Container Deployment](docker/multi_container.md)
