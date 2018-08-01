/**
 * Copyright (C) 2018 MongoDB Inc.
 *
 * This program is free software: you can redistribute it and/or  modify
 * it under the terms of the GNU Affero General Public License, version 3,
 * as published by the Free Software Foundation.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 * As a special exception, the copyright holders give permission to link the
 * code of portions of this program with the OpenSSL library under certain
 * conditions as described in each individual source file and distribute
 * linked combinations including the program with the OpenSSL library. You
 * must comply with the GNU Affero General Public License in all respects
 * for all of the code used other than as permitted herein. If you modify
 * file(s) with this exception, you may extend this exception to your
 * version of the file(s), but you are not obligated to do so. If you do not
 * wish to do so, delete this exception statement from your version. If you
 * delete this exception statement from all source files in the program,
 * then also delete it in the license file.
 */

#include "mongo/platform/basic.h"

#include "mongo/db/index/all_paths_key_generator.h"

#include "mongo/db/jsobj.h"
#include "mongo/db/query/collation/collation_index_key.h"

namespace mongo {
namespace {

// If the user does not specify any projection, then we default to a projection of {_id: 0} in order
// to prevent the _id field from being indexed, since it already has its own dedicated index.
static const BSONObj kDefaultProjection = BSON("_id"_sd << 0);

// If the enclosing object is an array, then the current element's fieldname is the array index, so
// we omit this when computing the full path. Otherwise, the full path is the pathPrefix plus the
// element's fieldname.
void pushPathComponent(BSONElement elem, bool enclosingObjIsArray, FieldRef* pathPrefix) {
    if (!enclosingObjIsArray) {
        pathPrefix->appendPart(elem.fieldNameStringData());
    }
}

// If the enclosing object is not an array, then the final path component should be its field name.
// Verify that this is the case and then pop it off the FieldRef.
void popPathComponent(BSONElement elem, bool enclosingObjIsArray, FieldRef* pathToElem) {
    if (!enclosingObjIsArray) {
        invariant(pathToElem->getPart(pathToElem->numParts() - 1) == elem.fieldNameStringData());
        pathToElem->removeLastPart();
    }
}
}  // namespace

constexpr StringData AllPathsKeyGenerator::kSubtreeSuffix;

std::unique_ptr<ProjectionExecAgg> AllPathsKeyGenerator::createProjectionExec(
    const BSONObj& keyPattern, const BSONObj& pathProjection) {
    // We should never have a key pattern that contains more than a single element.
    invariant(keyPattern.nFields() == 1);

    // The _keyPattern is either { "$**": ±1 } for all paths or { "path.$**": ±1 } for a single
    // subtree. If we are indexing a single subtree, then we will project just that path.
    auto indexRoot = keyPattern.firstElement().fieldNameStringData();
    auto suffixPos = indexRoot.find(kSubtreeSuffix);

    // If we're indexing a single subtree, we can't also specify a path projection.
    invariant(suffixPos == std::string::npos || pathProjection.isEmpty());

    // If this is a subtree projection, the projection spec is { "path.to.subtree": 1 }. Otherwise,
    // we use the path projection from the original command object. If the path projection is empty
    // we default to {_id: 0}, since empty projections are illegal and will be rejected when parsed.
    auto projSpec = (suffixPos != std::string::npos
                         ? BSON(indexRoot.substr(0, suffixPos) << 1)
                         : pathProjection.isEmpty() ? kDefaultProjection : pathProjection);

    // If the projection spec does not explicitly specify _id, we exclude it by default. We also
    // prevent the projection from recursing through nested arrays, in order to ensure that the
    // output document aligns with the match system's expectations.
    return ProjectionExecAgg::create(
        projSpec,
        ProjectionExecAgg::DefaultIdPolicy::kExcludeId,
        ProjectionExecAgg::ArrayRecursionPolicy::kDoNotRecurseNestedArrays);
}

AllPathsKeyGenerator::AllPathsKeyGenerator(BSONObj keyPattern,
                                           BSONObj pathProjection,
                                           const CollatorInterface* collator)
    : _collator(collator), _keyPattern(keyPattern) {
    _projExec = createProjectionExec(keyPattern, pathProjection);
}

void AllPathsKeyGenerator::generateKeys(BSONObj inputDoc,
                                        BSONObjSet* keys,
                                        MultikeyPathsMock* multikeyPaths) const {
    FieldRef workingPath;
    _traverseAllPaths(
        _projExec->applyProjection(inputDoc), false, &workingPath, keys, multikeyPaths);
}

void AllPathsKeyGenerator::_traverseAllPaths(BSONObj obj,
                                             bool objIsArray,
                                             FieldRef* path,
                                             BSONObjSet* keys,
                                             MultikeyPathsMock* multikeyPaths) const {
    for (const auto elem : obj) {
        // If this element is an empty object, fast-path skip it.
        if (elem.type() == BSONType::Object && elem.Obj().isEmpty())
            continue;

        // Append the element's fieldname to the path, if the enclosing object is not an array.
        pushPathComponent(elem, objIsArray, path);

        switch (elem.type()) {
            case BSONType::Array:
                // If this is a nested array, we don't descend it but instead index it as a value.
                if (_addKeyForNestedArray(elem, *path, objIsArray, keys))
                    break;

                // Add an entry for the multi-key path, and then fall through to BSONType::Object.
                _addMultiKey(*path, multikeyPaths);

            case BSONType::Object:
                _traverseAllPaths(
                    elem.Obj(), elem.type() == BSONType::Array, path, keys, multikeyPaths);
                break;

            default:
                _addKey(elem, *path, keys);
        }

        // Remove the element's fieldname from the path, if it was pushed onto it earlier.
        popPathComponent(elem, objIsArray, path);
    }
}

bool AllPathsKeyGenerator::_addKeyForNestedArray(BSONElement elem,
                                                 const FieldRef& fullPath,
                                                 bool enclosingObjIsArray,
                                                 BSONObjSet* keys) const {
    // If this element is an array whose parent is also an array, index it as a value.
    if (enclosingObjIsArray && elem.type() == BSONType::Array) {
        _addKey(elem, fullPath, keys);
        return true;
    }
    return false;
}

void AllPathsKeyGenerator::_addKey(BSONElement elem,
                                   const FieldRef& fullPath,
                                   BSONObjSet* keys) const {
    // AllPaths keys are of the form { "": "path.to.field", "": <collation-aware value> }
    BSONObjBuilder bob;
    bob.append("", fullPath.dottedField());
    CollationIndexKey::collationAwareIndexKeyAppend(elem, _collator, &bob);
    keys->insert(bob.obj());
}

void AllPathsKeyGenerator::_addMultiKey(const FieldRef& fullPath,
                                        MultikeyPathsMock* multikeyPaths) const {
    // Multikey paths are denoted by an entry of the form { "": 1, "": "path.to.array" }.
    multikeyPaths->insert(BSON("" << 1 << "" << fullPath.dottedField()));
}

}  // namespace mongo
