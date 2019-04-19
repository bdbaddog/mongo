# TODO: Filter child chains by role (install bin/foo should not install lib/dep.debug)
# TODO: Add test tag automatically for unit tests, etc.
# TODO: Test tag still leaves things in the runtime component
# TODO: But meta doesn't depend on test! Should it though?
# TODO: How should debug info work for tests?
# TODO: destdir vs prefix (what about --install-sandbox?)
# TODO: Versioned libraries
# TODO: library dependency chaining for windows dynamic builds, static dev packages
# TODO: Injectible component dependencies (jscore -> resmoke, etc.)
# TODO: Distfiles and equivalent for the dist target
# TODO: Handle chmod state
# TODO: tarfile generation
# TODO: Installing resmoke and configurations
# TODO: package decomposition
# TODO: Install/package target help text
# TODO: implement sdk_headers
# TODO: Namedtuple for alias_map

import itertools
from collections import defaultdict, namedtuple

import SCons


RoleInfo = namedtuple(
    'RoleInfo',
    [
        'alias_name',
        'alias',
        'prealias_name',
        'prealias',
    ],
)

def exists(_env):
    """Always activate this tool."""
    return True


def generate(env): # pylint: disable=too-many-statements
    """Generate the auto install builders."""

    env["INSTALLDIR_BINDIR"] = "$INSTALL_DIR/bin"
    env["INSTALLDIR_LIBDIR"] = "$INSTALL_DIR/lib"
    env["INSTALLDIR_INCLUDEDIR"] = "$INSTALL_DIR/include"

    role_tags = set(["common", "debug", "dev", "meta", "runtime"])

    role_dependencies = {
        "debug": [
            "runtime",
        ],
        "dev": [
            "runtime",
            "common",
        ],
        "meta": [
            "dev",
            "runtime",
            "common",
            "debug",
        ],
        "runtime": [
            "common",
        ],
    }

    env.Tool("install")

    # TODO: These probably need to be patterns of some sort, not just suffixes.
    # TODO: The runtime libs should be in runtime, the dev symlinks in dev
    suffix_map = {
        env.subst("$PROGSUFFIX") : (
            "$INSTALLDIR_BINDIR", [
                "runtime",
            ]
        ),

        env.subst("$LIBSUFFIX") : (
            "$INSTALLDIR_LIBDIR", [
                "dev",
            ]
        ),

        ".dll" : (
            "$INSTALLDIR_BINDIR", [
                "runtime",
            ]
        ),

        ".dylib" : (
            "$INSTALLDIR_LIBDIR", [
                "runtime",
                "dev",
            ]
        ),

        ".so" : (
            "$INSTALLDIR_LIBDIR", [
                "runtime",
                "dev",
            ]
        ),

        ".debug" : (
            "$INSTALLDIR_DEBUGDIR", [
                "debug",
            ]
        ),

        ".dSYM" : (
            "$INSTALLDIR_LIBDIR", [
                "runtime"
            ]
        ),

        ".lib" : (
            "$INSTALLDIR_LIBDIR", [
                "runtime"
            ]
        ),
    }


    def _aib_debugdir(source, target, env, for_signature):
        for s in source:
            # TODO: Dry this with auto_install_emitter
            # TODO: We shouldn't need to reach into the attributes of the debug tool like this.
            origin = getattr(s.attributes, "debug_file_for", None)
            oentry = env.Entry(origin)
            osuf = oentry.get_suffix()
            return suffix_map.get(osuf)[0]


    env["INSTALLDIR_DEBUGDIR"] = _aib_debugdir

    alias_map = defaultdict(dict)

    def auto_install(env, target, source, **kwargs):

        target = env.Dir(env.subst(target, source=source))

        # We want to make sure that the executor information stays
        # persisted for this node after it is built so that we can
        # access it in our install emitter below.
        source = list(map(env.Entry, env.Flatten([source])))
        for s in source:
            s.attributes.keep_targetinfo = 1

        actions = SCons.Script.Install(target=target, source=source)

        for s in source:
            s.attributes.aib_install_actions = actions

        roles = {
            kwargs.get("INSTALL_ROLE"),
            # The 'meta' tag is implicitly attached as a role.
            "meta",
        }
        components = {
            kwargs.get("INSTALL_COMPONENT"),
            # The 'all' tag is implicitly attached as a component
            "all",
        }

        # Remove false values such as None
        roles = {role for role in roles if role}
        components = {component for component in components if component}

        for component_tag, role_tag in itertools.product(components, roles):
            alias_name = "install-" + component_tag
            alias_name = alias_name + ("" if role_tag == "runtime" else "-" + role_tag)
            prealias_name = "pre" + alias_name
            alias = env.Alias(alias_name, actions)
            prealias = env.Alias(prealias_name, source)
            alias_map[component_tag][role_tag] = RoleInfo(
                alias_name=alias_name,
                alias=alias,
                prealias_name=prealias_name,
                prealias=prealias,
            )

        return actions

    env.AddMethod(auto_install, "AutoInstall")

    def finalize_install_dependencies(env):
        common_rolemap = alias_map.get("common", None)
        default_rolemap = alias_map.get("default", None)

        if default_rolemap and "runtime" in default_rolemap:
            env.Alias("install", "install-default")
            env.Default("install")

        for component, rolemap in alias_map.items():
            for role, info in rolemap.items():

                if common_rolemap and component != "common" and role in common_rolemap:
                    env.Depends(info.alias, common_rolemap[role].alias)
                    env.Depends(info.prealias, common_rolemap[role].prealias)

                for dependency in role_dependencies.get(role, []):
                    dependency_info = rolemap.get(dependency, [])
                    if dependency_info:
                        env.Depends(info.alias, dependency_info.alias)
                        env.Depends(info.prealias, dependency_info.prealias)

        installedFiles = env.FindInstalledFiles()
        env.NoCache(installedFiles)

    env.AddMethod(finalize_install_dependencies, "FinalizeInstallDependencies")

    def auto_install_emitter(target, source, env):
        for t in target:
            tentry = env.Entry(t)
            tsuf = tentry.get_suffix()
            auto_install_location = suffix_map.get(tsuf)
            if auto_install_location:
                tentry_install_tags = env.get("INSTALL_ALIAS", [])
                tentry_install_tags.extend(auto_install_location[1])
                setattr(tentry.attributes, "INSTALL_ALIAS", tentry_install_tags)
                env.AutoInstall(
                    auto_install_location[0], tentry, INSTALL_ALIAS=tentry_install_tags
                )
        return (target, source)

    def add_emitter(builder):
        base_emitter = builder.emitter
        new_emitter = SCons.Builder.ListEmitter([base_emitter, auto_install_emitter])
        builder.emitter = new_emitter

    target_builders = ["Program", "SharedLibrary", "LoadableModule", "StaticLibrary"]
    for builder in target_builders:
        builder = env["BUILDERS"][builder]
        add_emitter(builder)

    def scan_for_transitive_install(node, env, path=()):
        results = []
        install_sources = node.sources
        for install_source in install_sources:
            install_executor = install_source.get_executor()
            if not install_executor:
                continue
            install_targets = install_executor.get_all_targets()
            if not install_targets:
                continue
            for install_target in install_targets:
                grandchildren = install_target.children()
                for grandchild in grandchildren:
                    actions = getattr(
                        grandchild.attributes, "aib_install_actions", None
                    )
                    if actions:
                        results.extend(actions)
        results = sorted(results, key=str)
        return results

    from SCons.Tool import install

    base_install_builder = install.BaseInstallBuilder
    assert base_install_builder.target_scanner is None

    base_install_builder.target_scanner = SCons.Scanner.Scanner(
        function=scan_for_transitive_install, path_function=None
    )
