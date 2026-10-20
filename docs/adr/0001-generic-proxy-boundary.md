# Keep the proxy independent of applications

proxrs provides HTTP forwarding, HTTPS CONNECT, and a generic command launcher that supplies proxy environment variables. Codex and pi motivated the project, but application detection, provider authentication, background-service integration, and per-application compatibility certification would tie a reusable proxy to unrelated application lifecycles. Validate protocol and process behavior through generic clients and controlled Docker fixtures; leave application-specific networking to the consumer.
