# Windows local control uses restricted Named Pipes

Windows uses a per-user Named Pipe for the cc-connect Local Endpoint because it provides a same-machine byte stream with an enforceable Windows access boundary. TCP and WebSocket transports are rejected rather than used as fallbacks because loopback addressing does not isolate desktop users and would introduce a separate authentication surface.
