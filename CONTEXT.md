# Agent Chat Session Sync

This context connects an already-existing local Agent conversation to a dedicated chat without replacing either system's native session history.

## Language

**Native Agent Session**:
The conversation identity and history owned by Codex or Claude Code.
_Avoid_: Mirrored session, sync session

**Chat Binding**:
A durable association between one Native Agent Session and its dedicated platform chat within a configured Agent project and workspace.
_Avoid_: Session copy, chat cache

**Local Endpoint**:
The same-machine control boundary through which a Chat Binding is activated in cc-connect.
_Avoid_: Public API, remote endpoint

**Binding Replay**:
Reactivation of durable Chat Bindings after the process serving the Local Endpoint starts or is replaced.
_Avoid_: Session recreation, message replay

**Hook Receipt**:
A durable record that an Agent lifecycle event was accepted for later processing.
_Avoid_: Delivered message, transient callback
