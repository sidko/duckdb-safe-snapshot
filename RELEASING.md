# Releasing

After `main` CI passes, push the matching `v<version>` tag over SSH. The release
workflow checks out the event SHA, validates the version, builds the package,
and runs the clean-consumer test. PyPI retries compare normalized archive
contents, ignore container timestamps, and skip only matching files. A GitHub
Release follows a successful registry publish.
