# Own each client connection through HTTP and CONNECT

Keep httparse preflight, Hyper coordination, HTTP forwarding, CONNECT relaying, and request-associated errors inside one client-connection module, with a single runner shared by production and byte-stream tests and protocol limits kept internal, so ordering and lifetime knowledge concentrate in its implementation.
Its internal destination seam selects the current SSH connection before destination validation and opens once through that selection, with SSH and scripted stream adapters preserving error precedence and preventing a started request from migrating to a replacement connection or being replayed.
The runner owns bounded child futures through streaming and tunnel completion so dropping it drops its local work and streams; the listener retains admission and the client-capacity permit for that lifetime, while SSH supervision and asynchronous channel-close cleanup retain their existing owners.
