# Copyright 2025 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# buildifier: disable=module-docstring

# To update XLA to a new revision,
# a) update XLA_COMMIT to the new git commit hash
# b) get the sha256 hash of the commit by running:
#    curl -L https://api.github.com/repos/openxla/xla/tarball/{git_hash} | sha256sum
#    and update XLA_SHA256 with the result.
# Note: This is a clone of revision.bzl for testing the new automation for updating XLA in JAX.

# buildifier: disable=module-docstring
XLA_COMMIT = "1b9dc5062fb4a68a101d7e1ad4b59a88eecfc961"
XLA_SHA256 = "92c317617756b147551f77499728312fcbbd824154e2699a667250279ff2d358"
