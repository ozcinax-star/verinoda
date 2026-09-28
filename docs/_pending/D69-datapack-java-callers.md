# D69-datapack-java-callers (pending doc text for the operator to merge)

Section numbers are left as `NN` for the operator (the next free DESIGN.md section). GitHub issue #1.

## DESIGN section

## NN. Datapack functions called from Java (D69, 2026-09-28)

### NN.1 Why

`verinoda datapack function ns:path` listed only the callers in `.mcfunction` files, although the command's help
said "one tag, score or function across mcfunction and Java". In a mod the question "who calls this function?" is
usually about Java: where the game actually triggers it. The bug that raised the issue was of that kind: a
projectile weapon ran a function that acts on the one entity the datapack keeps track of instead of the body it
hit, and the answer to "who calls it?" named one mcfunction caller and neither of the two Java callers that
mattered. A
function run only by Java command strings was reported as "no call in or out found". A Java call to a function
that does not exist is a silent bug (`getFunctions().get` returns nothing and nothing happens), and the summary's
"calls to functions that do not exist" did not look at Java. D52 had listed "Java running a function by a built
string" as not done.

Java runs a datapack function in three ways, and all three occur in the mod that raised the issue:

1. a **command string**: `performPrefixedCommand(src, "function ns:x")`, `runCommand("execute ... run function
   ns:x")`, Bukkit's `dispatchCommand(console, "function ns:x")`;
2. an **identifier** handed to the function manager: `server.getFunctions().get(Identifier.fromNamespaceAndPath(
   "ns", "x"))`. The same `fromNamespaceAndPath` builds item models, textures, dimensions and registry keys, so the
   identifier alone proves nothing;
3. a **helper** that adds the namespace: `static void runFunction(ServerPlayer p, String name)` whose body is form
   2 with `name` as the path; the code then only says `runFunction(p, "x")`. The mod has three same-named helpers
   (a static one with two parameters, a private one with three, a command-context one taking `ns:x` or `x`), so a
   helper cannot be recognised by its name.

### NN.2 Decisions

- **`verinoda/datapack_java.py`** reads the Java files with tree-sitter (the Java grammar is already a dependency).
  A file is parsed only when its text names the function manager (`getFunctions`, `getCommandFunctionManager`)
  or holds a string with `function ` in it, or calls a helper found so far; the nodes are found from the text
  (the regex's byte offset, then the node there), not by walking every node. `verinoda datapack` and `datapack
  function` run it; `datapack tag` / `score` do not.
- **A command string must read as a command.** A `+` concatenation is joined into one text, a part known only at
  run time (`p.getName()`) kept as a placeholder, a `static final String` constant filled in, a text block read
  line by line. The text must start as a command (`function`, `execute`, `schedule`, `return`, a leading `/`
  allowed) or with a run-time part followed by an `execute` step (`... + " run function ns:x"`); after the id only
  the end, macro arguments (`{...}`, `with ...`) or, for `schedule`, a time may follow. So a log format
  (`"function ns:{} does not exist"`), a chat hint (`"Cleanup: /function ns:x"`), a sentence that mentions a
  command (`"the /execute ... run function ns:x retry"`) and a comment are not calls. The record says how:
  `function`, `execute run` or `schedule`.
- **An identifier counts only in the function manager's lookup.** A `get(...)` / `getFunction(...)` whose receiver
  is `getFunctions()` / `getCommandFunctionManager()` or a local holding it (`ServerFunctionManager functions =
  server.getFunctions()`, by its initialiser or its declared type); its argument is the identifier, inline or
  through a local assigned once (`Identifier id = ...; get(id)`). Identifiers: `Identifier` / `ResourceLocation`
  `fromNamespaceAndPath`, `of`, `tryBuild`, `parse`, `tryParse`, `withDefaultNamespace` and `new ...(ns, path)`,
  fully qualified or not, over several lines. The same identifier as an item model or a registry key is not a
  call.
- **A helper is recognised by its body**: a method whose String parameter reaches that lookup with a constant
  namespace (`fromNamespaceAndPath(NS, name)`, `"ns:" + name`, `"ns:prefix_" + name`, the whole-id-or-path form
  `name.contains(":") ? name : "ns:" + name`), or completes the name in a command string (`"function ns:" +
  name`, `String.format("function ns:%s", name)`). A helper is its own declaration (file and position), so two
  same-named helpers, or two same-named classes in two packages or two trees, stay apart. A call binds to a
  helper only when Java would resolve it there: a qualified call (`Mod.runFunction(...)`, `pkg.Mod.runFunction`,
  the declared type of an instance receiver) when the caller's file can see that class (the same file or
  package, `import pkg.Mod;`, `import pkg.*;`, a fully qualified name, `Outer.Inner` for a nested class); an
  unqualified call when the innermost enclosing class that declares a method of that name declares the helper
  with that argument count (a same-named method of the caller's own class is not the helper); or a static
  import of it. A helper found by its body binds only calls in its own tree: the reference tree may repeat the
  product's class and package names. A method that passes its own parameter to a helper is a helper too (a few
  rounds), an overload that delegates to the helper (`runFunction(p, name, delay)` calling `runFunction(p,
  name)`) included; only a call in the helper's own body is not. Every call of a helper with a constant (a
  literal, a constant, a local assigned once from one) is a call of the function the helper builds from it; so
  is a call inside a loop over a constant table (`for (String[] e : TABLE) ... e[1]`, `final String fn = e[1]`,
  `for (String n : List.of(...))`, the table in a local or a final field of the file): one record per distinct
  function, at the line of the call. `datapack.function_helpers` (or `function-helpers`) in
  `.verinoda/config.json`, `["Class.method:argIndex:namespace"]`, names a helper whose body is beyond this
  reading.
- **A name built at run time is reported, never dropped** (`"give_" + kind`, a loop over names from elsewhere, a
  command argument, `String.format("function ns:%s", kind)` / `.formatted`): the summary lists each site once as
  `dynamic: File.java:N builds ns:give_*` (or "builds the whole name at run time"), and `datapack function
  ns:give_sword` lists the dynamic sites whose known part fits it as "may be this one" (also when the datapacks
  do not have the function). A bare `ns:*` (or a whole id built at run time) fits every function: the function
  view gives one line naming those sites ("N Java site(s) build the name at run time and may run this one too")
  instead of saying "no call in or out found".
- **Lines.** A helper call is reported at the line of its name (a chained call split after `Mod` at the line
  of `.runFunction(`); each command of a text block at its own line. `execute if|unless function ns:x run ...`
  runs `ns:x` as the condition and counts, as it does in an mcfunction file. A `static final Identifier` field
  handed to the lookup is read like a local.
- **Cost.** A file is read for helper calls only when its text calls one of the helpers' names (not the name
  inside another word) and names that helper's class, so a helper called `get` or `run` does not make every
  file with `.get(` a candidate.
- **Output.** `datapack function` lists each Java call as its own record after the mcfunction callers:
  `called by Java Class.method at path/File.java:N (helper Mod.runFunction)` / `(command string, execute run)` /
  `(identifier)`, with `[test]` for a test file and `[reference tree NAME]` for a file under a configured reference
  tree (`index.reference`, `setup --reference`: the same rules, labelled with the tree's path); product code first,
  then tests, then reference trees. JSON: `called_by` rows carry `kind` (`mcfunction` or `java`); a Java row has
  `via` (`command-string`, `identifier`, `helper`), `helper`, `caller`, `at`, `how`, `target`, `text`, and `tree`
  / `test` when they apply; `dynamic` lists the run-time names that may be this function and `dynamic_any` the
  sites that build all of the name past the namespace. "no call in or out found" is said only when there is no
  mcfunction caller, no Java caller, no event and no dynamic site that fits. A function the datapacks do not have
  but Java calls prints its Java callers (and the fitting dynamic sites) under "not found" (exit 2).
- **Summary.** The head counts the Java calls and, when some are in tests or reference trees, how many are in
  product code; "calls to functions that do not exist" includes a Java call whose
  target is missing, in a namespace the datapacks define (another mod's functions and `minecraft:` ids are not
  flagged); a line gives the Java calls by kind (and how many are in tests and reference trees) and the helpers
  recognised; the names built at run time follow. JSON: `java` (`calls`, `by_via`, `in_tests`,
  `in_reference_trees`, `helpers` with where each was found and why, `dynamic`, `unresolved_lookups`).
- **Not in the graph.** The Java calls are read at query time and are not graph edges: `when`, `trace` and impact
  still follow only the mcfunction calls.

### NN.3 Measured

On a copy of the private mod that raised the issue (one datapack namespace, 314 functions; 425 Java files of which
41 in a configured reference tree, the Paper plugin it was ported from), the real CLI:

- **Before** (849ca63): no `datapack function` answer listed a Java caller. For the four functions the issue
  names, 0 of their 7 Java callers were listed, and one of the four was "no call in or out found".
- **After**: 68 Java calls bound to a function: 23 in product code (helper 18, command string 3, identifier 2), 34
  in tests (helper 31, command string 2, identifier 1), 11 in the reference tree (command strings). 8 names built
  at run time are listed as dynamic (5 in product code, 1 in tests, 2 in the reference tree); 0 lookups of an id the
  text does not spell. 10 helpers recognised by their body, none configured: in product code the static
  two-parameter helper, the private three-parameter helper of the same name, the command-context helper and one
  method that hands its parameter on; 6 in tests (one takes the function as its fourth of six parameters).
  9 of the 68 come from constant tables: the stage command's "spawn" verbs (a local `String[][]` of 7 rows naming
  4 functions, run through the command-context helper) and a test's `List.of` of 5 function names; before the
  review fixes they were 2 bare `ns:*` dynamic sites. The other 59 calls and their lines did not change.
- **The four functions**: 7 of 7 Java callers listed with file:line and the calling method (the two callers
  through the static helper, the three command strings, the one identifier lookup, the one call through the
  private three-parameter helper), plus 1 test caller and 5 reference-tree callers of the same functions.
- **False positives**: every one of the first 59 bound calls read by hand, and the 9 table rows checked against
  the tables: 0 wrong, and all 68 targets exist (0 missing functions from Java). The lines that must not count do
  not: identifiers built for item models and a registry lookup of an identifier, a sentence that mentions `run
  function`, a log format string, a chat hint with `/function`, and comments that quote a command. The issue
  counted "~40" by text search (helper calls, every `fromNamespaceAndPath` of the namespace, every `function ns:`
  string); in product code the syntax gives 20 helper call sites (14 with a constant, 1 over a constant table, 4
  built at run time, 1 that forwards a parameter), 3 identifier lookups (the
  other matches build item models, textures or dimensions, or are the helpers' own bodies) and 3 command strings
  (the other matches were a comment, a doc comment and a chat hint).
- **Time**: `verinoda datapack` 2.28 s -> 2.60 s, `datapack function` 2.13 s -> 2.49 s (median of three, Windows);
  31 of the 425 Java files are parsed, 0.27 s. The first version walked every node of 46 files (0.9 s); finding
  the nodes from the text instead gave the same 59 calls and 10 dynamic sites. The review fixes keep the 31 files
  (the scan 0.26 s -> 0.29 s in process; the CLI's change is inside its run-to-run noise). With one extra helper
  given a common name, the files read went from 142 / 229 / 273 / 371 (`fire` / `run` / `add` / `get`) to 32 /
  32 / 45 / 45.

### NN.4 Not done

Java calls as graph edges (`when` / `trace` from a Java method to a function; the MCP `analyze` / `node_inspect`
still see the mcfunction callers only), Kotlin, advancement rewards and predicates. A table built by code rather
than written out (`map.put(...)` in a loop, a table in another file) is still dynamic; a helper inherited from a
superclass in another file is not bound on an unqualified call. A command string is reported where its text is
(a `static final String` command at its declaration, not at each dispatch), and a string that holds a command is a
call whether or not it reaches the dispatcher (`assertEquals("function ns:x", s)`); an existence check
(`getFunctions().get(id).isPresent()`) counts as a lookup.

## Decision table row

| D69 | Datapack functions called from Java | implemented | Built 2026-09-28 (section NN, GitHub issue #1): `datapack_java.py`; `verinoda datapack function` lists Java callers as their own records (`via`: command string that reads as a command, the function manager's lookup of an identifier, or a helper recognised by its body and keyed by class, name and argument count; `datapack.function_helpers` for others; a call binds only where Java resolves it: imports, packages, nested classes, the caller's tree), labelled `[test]` and `[reference tree NAME]`; constant tables and `String.format` are read; names built at run time are listed as dynamic (`ns:prefix_*`); a Java call to a missing function is a mismatch. A private mod: 0 -> 68 Java calls, the issue's four functions 0/7 -> 7/7 Java callers, 0 false positives, +0.3 s. Not done: graph edges, Kotlin. |

## UPGRADING note

Add to "Upgrading from 0.3.2 (D60-D69)":

### D69: Datapack functions called from Java

Nothing to run: `verinoda datapack` reads the Java at query time, the index is unchanged. `datapack function`
now lists Java callers after the mcfunction ones, and its JSON changed: each `called_by` row has `kind`
(`mcfunction` or `java`; a Java row has `via`, `helper`, `caller`, `at`, `how`, `target`, `text` and, when they
apply, `tree` and `test`), a new `dynamic` list holds the names Java builds at run time that may be the
function (also in a `not_found` answer), and `dynamic_any` the sites that build all of the name past the
namespace. The summary JSON has `java_calls` and `java`, and a `missing_functions` row from Java has `how: "java
<via>"`, `caller` and `helper` / `tree` when they apply. A helper whose body Verinoda cannot read can be named in
`.verinoda/config.json`: `{"datapack": {"function_helpers": ["Class.method:argIndex:namespace"]}}`.
