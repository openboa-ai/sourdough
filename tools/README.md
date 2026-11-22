# On-device employee for the first paper trade

This macOS employee adapter uses the installed on-device system model. It
does not request credentials, use a hosted model, or receive company or venue
database paths. A model tool invocation proposes an order; Sourdough alone
decides admission, dispatches and books the independently observed fill.

Compile the adapter in the project's external task temporary directory:

```sh
swiftc -swift-version 6 -parse-as-library tools/PaperEmployee.swift -o "$TMPDIR/paper-employee"
printf '%s' '{"operation":"ready"}' | "$TMPDIR/paper-employee"
```

Do not launch a duty if readiness is false. For an authorized local trial,
use `sourdough employee-profile` on that executable and use the returned
profile when founding the trial company. Bind a fixed-quote paper account
with `sourdough bind-paper` and separate company and venue stores. Run
`sourdough first-duty` with that same executable. Never replace it with the
scripted CI peer for actual-run acceptance.

A hold is valid but is not a trade. A pass requires the model's tool
invocation, matching order and fill in the separate venue journal, and the
resulting position in the company store. Record the tested source revision,
adapter digest, macOS version and actual system-model binding. This does not
qualify live routing or market-based performance.
