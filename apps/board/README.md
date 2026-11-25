# Sourdough Board Client

The native macOS Client renders the company's read-only work projection. It does not start the company, access SQLite directly, send orders, or govern risk in this unit.

Use the versions and SHA-256 pins in `dependencies.lock`: Zig 0.16.0, the macOS arm64 release CLI and source for Native SDK 0.11.1, and the specified Yogurt source archive. Set `NATIVE_SDK_PATH` to the verified SDK source, add the corresponding CLI and Zig to `PATH`, and run these commands from this directory:

```sh
/bin/sh prepare-ui.sh /absolute/path/to/yogurt.tar.gz
native test .
native check . --strict
native build .
```

The installer copies the supported native button and its theme and font assets. It also brings the Sourdough app icon and font notice. None of those assets or the SDK is required by the company service.

To observe a company, set `SOURDOUGH_SERVICE` to the absolute path to the installed Sourdough service executable, `SOURDOUGH_STORE` to its existing company store path, and `SOURDOUGH_COMPANY` to its company ID before launching the Client. These settings are local bindings, not a grant. The service authorizes every inspection. With no binding or with a failed query, the screen does not retain old company data.
