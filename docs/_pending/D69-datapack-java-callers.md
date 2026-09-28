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
  name`). A helper is keyed by class, name and argument count; a call binds to the one its qualifier names
  (`Mod.runFunction(...)`), or its own class for an unqualified call, or the declared type of an instance
  receiver, with the right argument count; one in the caller's reference tree is preferred. A method that passes
  its own parameter to a helper is a helper too (a few rounds). Every call of a helper with a constant (a literal,
  a constant, a local assigned once from one) is a call of the function the helper builds from it.
  `datapack.function_helpers` (or `function-helpers`) in `.verinoda/config.json`, `["Class.method:argIndex:namespace"]`,
  names a helper whose body is beyond this reading.
- **A name built at run time is reported, never dropped** (`"give_" + kind`, a loop variable, a command argument):
  the summary lists each site once as `dynamic: File.java:N builds ns:give_*` (or "builds the whole name at run
  time"), and `datapack function ns:give_sword` lists the dynamic sites whose known part fits it as "may be this
  one". A bare `ns:*` fits every function and is said in the summary only.
- **Output.** `datapack function` lists each Java call as its own record after the mcfunction callers:
  `called by Java Class.method at path/File.java:N (helper Mod.runFunction)` / `(command string, execute run)` /
  `(identifier)`, with `[test]` for a test file and `[reference tree NAME]` for a file under a configured reference
  tree (`index.reference`, `setup --reference`: the same rules, labelled with the tree's path); product code first,
  then tests, then reference trees. JSON: `called_by` rows carry `kind` (`mcfunction` or `java`); a Java row has
  `via` (`command-string`, `identifier`, `helper`), `helper`, `caller`, `at`, `how`, `target`, `text`, and `tree`
  / `test` when they apply; `dynamic` lists the run-time names that may be this function. "no call in or out
  found" is said only when there is no mcfunction caller, no Java caller, no event and no fitting dynamic site. A
  function the datapacks do not have but Java calls prints its Java callers under "not found" (exit 2).
- **Summary.** The head counts the Java calls; "calls to functions that do not exist" includes a Java call whose
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
- **After**: 59 Java calls bound to a function: 19 in product code (helper 14, command string 3, identifier 2), 29
  in tests (helper 26, command string 2, identifier 1), 11 in the reference tree (command strings). 10 names built
  at run time are listed as dynamic (6 in product code, 2 in tests, 2 in the reference tree); 0 lookups of an id the
  text does not spell. 10 helpers recognised by their body, none configured: in product code the static
  two-parameter helper, the private three-parameter helper of the same name, the command-context helper and one
  method that hands its parameter on; 6 in tests (one takes the function as its fourth of six parameters).
- **The four functions**: 7 of 7 Java callers listed with file:line and the calling method (the two callers
  through the static helper, the three command strings, the one identifier lookup, the one call through the
  private three-parameter helper), plus 1 test caller and 5 reference-tree callers of the same functions.
- **False positives**: every one of the 59 bound calls read by hand: 0 wrong, and all 59 targets exist (0 missing
  functions from Java). The lines that must not count do not: identifiers built for item models and a registry
  lookup of an identifier, a sentence that mentions `run function`, a log format string, a chat hint with
  `/function`, and comments that quote a command. The issue counted "~40" by text search (helper calls, every
  `fromNamespaceAndPath` of the namespace, every `function ns:` string); in product code the syntax gives 20 helper
  call sites (14 with a constant, 5 built at run time, 1 that forwards a parameter), 3 identifier lookups (the
  other matches build item models, textures or dimensions, or are the helpers' own bodies) and 3 command strings
  (the other matches were a comment, a doc comment and a chat hint).
- **Time**: `verinoda datapack` 2.28 s -> 2.60 s, `datapack function` 2.13 s -> 2.49 s (median of three, Windows);
  31 of the 425 Java files are parsed, 0.27 s. The first version walked every node of 46 files (0.9 s); finding
  the nodes from the text instead gave the same 59 calls and 10 dynamic sites.

### NN.4 Not done

Java calls as graph edges (`when` / `trace` from a Java method to a function), Kotlin, a name taken from a
constant array (`for (String[] e : TABLE) ... helper(ctx, e[1])` is reported as dynamic), `String.format` ids,
advancement rewards and predicates.

## Decision table row

| D69 | Datapack functions called from Java | implemented | Built 2026-09-28 (section NN, GitHub issue #1): `datapack_java.py`; `verinoda datapack function` lists Java callers as their own records (`via`: command string that reads as a command, the function manager's lookup of an identifier, or a helper recognised by its body and keyed by class, name and argument count; `datapack.function_helpers` for others), labelled `[test]` and `[reference tree NAME]`; names built at run time are listed as dynamic (`ns:prefix_*`); a Java call to a missing function is a mismatch. A private mod: 0 -> 59 Java calls, the issue's four functions 0/7 -> 7/7 Java callers, 0 false positives in 59 read, +0.3 s. Not done: graph edges, Kotlin. |

## UPGRADING note

Add to "Upgrading from 0.3.2 (D60-D69)":

### D69: Datapack functions called from Java

Nothing to run: `verinoda datapack` reads the Java at query time, the index is unchanged. `datapack function`
now lists Java callers after the mcfunction ones, and its JSON changed: each `called_by` row has `kind`
(`mcfunction` or `java`; a Java row has `via`, `helper`, `caller`, `at`, `how`, `target`, `text` and, when they
apply, `tree` and `test`), and a new `dynamic` list holds the names Java builds at run time that may be the
function. The summary JSON has `java_calls` and `java`, and a `missing_functions` row from Java has `how: "java
<via>"`, `caller` and `helper` / `tree` when they apply. A helper whose body Verinoda cannot read can be named in
`.verinoda/config.json`: `{"datapack": {"function_helpers": ["Class.method:argIndex:namespace"]}}`.
