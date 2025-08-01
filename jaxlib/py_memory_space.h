/* Copyright 2024 The JAX Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#ifndef JAXLIB_PY_MEMORY_SPACE_H_
#define JAXLIB_PY_MEMORY_SPACE_H_

#include <Python.h>

#include "absl/strings/string_view.h"
#include "nanobind/nanobind.h"
#include "jaxlib/nb_class_ptr.h"
#include "jaxlib/py_client.h"
#include "xla/python/ifrt/memory.h"

namespace xla {

class PyMemorySpace {
 public:
  PyMemorySpace(jax::nb_class_ptr<PyClient> client, ifrt::Memory* memory_space);

  // Memory spaces are compared using Python object identity, so we don't allow
  // them to be copied or moved.
  PyMemorySpace(const PyMemorySpace&) = delete;
  PyMemorySpace(PyMemorySpace&&) = delete;
  PyMemorySpace& operator=(const PyMemorySpace&) = delete;
  PyMemorySpace& operator=(PyMemorySpace&&) = delete;

  const jax::nb_class_ptr<PyClient>& client() const { return client_; }
  ifrt::Memory* memory_space() const { return memory_; }

  int process_index() const;
  absl::string_view platform() const;
  absl::string_view kind() const;

  absl::string_view Str() const;
  absl::string_view Repr() const;

  nanobind::list AddressableByDevices() const;

  static void RegisterPythonType(nanobind::module_& m);

 private:
  static int tp_traverse(PyObject* self, visitproc visit, void* arg);
  static int tp_clear(PyObject* self);
  static PyType_Slot slots_[];

  jax::nb_class_ptr<PyClient> client_;
  ifrt::Memory* memory_;
};

}  // namespace xla

#endif  // JAXLIB_PY_MEMORY_SPACE_H_
