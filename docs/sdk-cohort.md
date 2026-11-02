# Dashboard SDK cohort

The `gui` and `dev` extras both require `scitex-sdk[gui]==0.3.18`.
The dashboard consumes the shared header, filter and user-scope contracts;
SDK 0.3.18 also fixes phone content sizing and confines utility gestures to
shell-owned sidebars, preserving nested app panes.

An exact shared pin prevents test and deployed renderers from silently using
different SDK behavior. The runtime dependency contract checks that both extras
carry the same exact requirement and that the installed SDK satisfies it.
Existing Django header/filter/viewport checks exercise the consumer contract.

When preparing an unpublished SDK cohort, qualify an independently verified
SDK wheel alongside the normal `.[all,dev]` installation in an owned environment
with dependency resolution enabled. A local wheel is candidate evidence;
normal PyPI resolution and publication must be verified separately before
claiming the public cohort is installable. Do not bypass dependency checks.
