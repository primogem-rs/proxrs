# proxrs

Language for creating and using a proxy connection through a remote SSH server.

## Language

**SSH server**:
The remote host that authenticates the user and connects to destinations on the user's behalf.
_Avoid_: Proxy server (when referring to the SSH server), node

**Local proxy**:
The endpoint on the user's computer through which applications send proxied requests.
_Avoid_: Remote proxy, VPN

**Client connection**:
One connection from an application to the local proxy, carrying HTTP requests or becoming a CONNECT tunnel. A proxy session can contain many client connections, each lasting through any resulting tunnel.
_Avoid_: Proxy session (for a single client connection), SSH connection

**Proxy exclusion**:
A destination rule that tells an application to connect directly instead of using the local proxy.
_Avoid_: Exception (without specifying what is bypassed)

**Destination**:
The service an application wants to reach through the local proxy and SSH server.
_Avoid_: Target (which can be confused with the SSH server)

**Proxy session**:
One invocation's lifetime, from connecting to the SSH server through shutdown, including any reconnection attempts.
_Avoid_: Deployment, installation

**Managed command**:
The foreground application launched by proxrs whose proxy settings and lifetime belong to that proxy session. Independently running services are outside this ownership.
_Avoid_: Agent (applications other than coding agents are supported)
