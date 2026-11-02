# Governance

GRAM-402 is an open protocol with a reference implementation, licensed MIT.

**Maintainer.** Aleksandr Shcherbakov ([@miinimal](https://github.com/miinimal)) maintains the project and has
final say on merges and releases. Development uses AI coding assistants; every change is reviewed and tested before
it reaches `main`.

**Protocol changes.** Changes to message formats or schemes (`SPEC.md`) are proposed as an issue or pull request
with the reasoning and a compatibility note. A scheme, once released, is only extended in a backward-compatible
way; a breaking change gets a new scheme name or `x402Version`.

**Contributions.** Pull requests are welcome. They must keep CI green and add tests for new behaviour; payment code
needs tests for the abuse cases it touches. By contributing you agree to license your work under MIT.

**Releases.** `main` is always releasable. Security fixes ship first (see `SECURITY.md`).

**Decisions in the open.** Design decisions and their reasons are recorded in issues, pull requests and commit
messages, so anyone can see why the protocol is the way it is.
