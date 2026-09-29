using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Architecture;
using DesignAutomationFramework;

namespace TakedownExport
{
    public class ExportParams
    {
        public List<string> levels { get; set; } = new List<string>();
        public bool includeLinks { get; set; } = true;
        public bool includeArchColumns { get; set; } = true;
        public bool exportViews { get; set; } = true;
        public string hostFile { get; set; }
    }

    public class TakedownExportApp : IExternalDBApplication
    {
        public ExternalDBApplicationResult OnStartup(ControlledApplication application)
        {
            DesignAutomationBridge.DesignAutomationReadyEvent += OnDesignAutomationReady;
            return ExternalDBApplicationResult.Succeeded;
        }

        public ExternalDBApplicationResult OnShutdown(ControlledApplication application)
        {
            return ExternalDBApplicationResult.Succeeded;
        }

        private void OnDesignAutomationReady(object sender, DesignAutomationReadyEventArgs e)
        {
            e.Succeeded = false;
            var workDir = Directory.GetCurrentDirectory();
            var exporter = new Exporter(workDir);
            try
            {
                e.Succeeded = exporter.Run(e.DesignAutomationData.RevitApp, e.DesignAutomationData.RevitDoc);
            }
            catch (Exception ex)
            {
                exporter.Log("FATAL: " + ex);
            }
            finally
            {
                exporter.FlushLog();
            }
        }
    }

    internal class Source
    {
        public Document Doc;
        public Transform Xf;
        public string Name;
    }

    public class Exporter
    {
        private static readonly Regex CoreRoomName = new Regex(@"ELEV|STAIR|SHAFT|LIFT|CORE|HOIST", RegexOptions.IgnoreCase);
        private static readonly Regex ElevatorFamily = new Regex(@"ELEVATOR|LIFT", RegexOptions.IgnoreCase);

        private readonly string _workDir;
        private readonly string _resultDir;
        private readonly List<string> _log = new List<string>();
        private readonly List<string> _warnings = new List<string>();

        public Exporter(string workDir)
        {
            _workDir = workDir;
            _resultDir = Path.Combine(workDir, "result");
            Directory.CreateDirectory(_resultDir);
        }

        public void Log(string message)
        {
            var line = $"[{DateTime.UtcNow:HH:mm:ss}] {message}";
            Console.WriteLine(line);
            _log.Add(line);
        }

        public void FlushLog()
        {
            File.WriteAllLines(Path.Combine(_resultDir, "log.txt"), _log);
        }

        private void Warn(string message)
        {
            _warnings.Add(message);
            Log("WARNING: " + message);
        }

        public bool Run(Application app, Document providedDoc)
        {
            var p = ReadParams();
            var doc = providedDoc ?? OpenInput(app, p);
            if (doc == null)
            {
                Log("No input model found.");
                return false;
            }
            Log($"Opened '{doc.Title}' (workshared={doc.IsWorkshared}, app {app.VersionNumber} build {app.VersionBuild})");

            var links = ResolveLinks(doc, p);
            var sources = new List<Source> { new Source { Doc = doc, Xf = Transform.Identity, Name = "host" } };
            if (p.includeLinks)
            {
                foreach (var inst in new FilteredElementCollector(doc).OfClass(typeof(RevitLinkInstance)).Cast<RevitLinkInstance>())
                {
                    var linkDoc = inst.GetLinkDocument();
                    if (linkDoc != null)
                        sources.Add(new Source { Doc = linkDoc, Xf = inst.GetTotalTransform(), Name = "link:" + linkDoc.Title });
                }
            }

            var levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>()
                .OrderBy(l => l.ProjectElevation).ToList();
            var wanted = new HashSet<string>(p.levels ?? new List<string>(), StringComparer.OrdinalIgnoreCase);

            var result = new Dictionary<string, object>
            {
                ["schema"] = "takedown-revit/1",
                ["units"] = "ft",
                ["exporter"] = new { version = "1.0.0", revitVersion = app.VersionNumber, revitBuild = app.VersionBuild },
                ["document"] = new { title = doc.Title, pathName = doc.PathName, isWorkshared = doc.IsWorkshared },
                ["links"] = links,
                ["levels"] = levels.Select(l => new { id = Id(l.Id), name = l.Name, elevation = l.ProjectElevation }).ToList(),
                ["floors"] = sources.SelectMany(s => SafeCollect(s, CollectFloors)).ToList(),
                ["columns"] = sources.SelectMany(s => SafeCollect(s, src => CollectColumns(src, p))).ToList(),
                ["walls"] = sources.SelectMany(s => SafeCollect(s, CollectWalls)).ToList(),
                ["cores"] = sources.SelectMany(s => SafeCollect(s, CollectCoreMarkers)).ToList(),
                ["openings"] = sources.SelectMany(s => SafeCollect(s, CollectShaftOpenings)).ToList(),
                ["grids"] = sources.SelectMany(s => SafeCollect(s, CollectGrids)).ToList(),
            };

            var exportLevels = levels.Where(l => wanted.Count == 0 || wanted.Contains(l.Name)).ToList();
            result["views"] = p.exportViews ? ExportPlanViews(doc, exportLevels) : new List<object>();
            result["warnings"] = _warnings;

            var json = JsonSerializer.Serialize(result);
            File.WriteAllText(Path.Combine(_resultDir, "takedown.json"), json);
            Log($"Wrote takedown.json ({json.Length / 1024} KB)");
            return true;
        }

        private ExportParams ReadParams()
        {
            var path = Path.Combine(_workDir, "params.json");
            if (!File.Exists(path)) return new ExportParams();
            try
            {
                return JsonSerializer.Deserialize<ExportParams>(File.ReadAllText(path)) ?? new ExportParams();
            }
            catch (Exception ex)
            {
                Warn("params.json unreadable, using defaults: " + ex.Message);
                return new ExportParams();
            }
        }

        private List<string> FindModels()
        {
            return Directory.EnumerateFiles(_workDir, "*.rvt", SearchOption.AllDirectories)
                .Where(f => !f.StartsWith(_resultDir, StringComparison.OrdinalIgnoreCase))
                .Where(f => !Regex.IsMatch(Path.GetFileName(f), @"\.\d{4}\.rvt$", RegexOptions.IgnoreCase))
                .ToList();
        }

        private Document OpenInput(Application app, ExportParams p)
        {
            var models = FindModels();
            if (models.Count == 0) return null;
            var host = models.FirstOrDefault(f => !string.IsNullOrEmpty(p.hostFile) &&
                                                  Path.GetFileName(f).Equals(p.hostFile, StringComparison.OrdinalIgnoreCase))
                       ?? models.OrderByDescending(f => new FileInfo(f).Length).First();

            var info = BasicFileInfo.Extract(host);
            Log($"Host '{Path.GetFileName(host)}': saved in Revit {info.Format}, workshared={info.IsWorkshared}, " +
                $"{new FileInfo(host).Length / (1024 * 1024)} MB");

            var options = new OpenOptions { Audit = false };
            if (info.IsWorkshared)
            {
                options.DetachFromCentralOption = DetachFromCentralOption.DetachAndPreserveWorksets;
                options.SetOpenWorksetsConfiguration(new WorksetConfiguration(WorksetConfigurationOption.OpenAllWorksets));
            }
            return app.OpenDocumentFile(ModelPathUtils.ConvertUserVisiblePathToModelPath(host), options);
        }

        private List<object> ResolveLinks(Document doc, ExportParams p)
        {
            var models = FindModels();
            var links = new List<object>();
            foreach (var linkType in new FilteredElementCollector(doc).OfClass(typeof(RevitLinkType)).Cast<RevitLinkType>())
            {
                string savedPath = null;
                try
                {
                    var reference = linkType.GetExternalFileReference();
                    savedPath = ModelPathUtils.ConvertModelPathToUserVisiblePath(reference.GetAbsolutePath());
                }
                catch (Exception) { }

                var status = linkType.GetLinkedFileStatus();
                string loadedFrom = null;
                if (p.includeLinks && status != LinkedFileStatus.Loaded)
                {
                    var wantedName = Path.GetFileName(savedPath ?? linkType.Name);
                    var local = models.FirstOrDefault(f => Path.GetFileName(f).Equals(wantedName, StringComparison.OrdinalIgnoreCase));
                    if (local != null)
                    {
                        try
                        {
                            linkType.LoadFrom(ModelPathUtils.ConvertUserVisiblePathToModelPath(local), new WorksetConfiguration());
                            status = linkType.GetLinkedFileStatus();
                            loadedFrom = Path.GetFileName(local);
                        }
                        catch (Exception ex)
                        {
                            Warn($"Link '{linkType.Name}' found in upload but failed to load: {ex.Message}");
                        }
                    }
                }
                if (status != LinkedFileStatus.Loaded)
                    Warn($"Link '{linkType.Name}' not loaded ({status}); its elements are missing. Upload a zip with the host and this file.");
                links.Add(new { name = linkType.Name, savedPath, status = status.ToString(), loadedFrom });
            }
            return links;
        }

        private IEnumerable<object> SafeCollect(Source s, Func<Source, IEnumerable<object>> collect)
        {
            try
            {
                return collect(s).ToList();
            }
            catch (Exception ex)
            {
                Warn($"{collect.Method.Name} failed for {s.Name}: {ex.Message}");
                return new List<object>();
            }
        }

        private static long Id(ElementId id) => id?.Value ?? -1;

        private static string LevelName(Document d, ElementId id)
        {
            return id != null && id != ElementId.InvalidElementId ? (d.GetElement(id) as Level)?.Name : null;
        }

        private static double[] Xy(XYZ p) => new[] { Math.Round(p.X, 5), Math.Round(p.Y, 5) };

        private static List<double[]> Tessellate(IEnumerable<Curve> curves, Transform xf)
        {
            var pts = new List<double[]>();
            foreach (var curve in curves)
            {
                var seg = curve.Tessellate();
                for (var i = 0; i < seg.Count - 1; i++) pts.Add(Xy(xf.OfPoint(seg[i])));
            }
            return pts;
        }

        private static double[] ZRange(Element e, Transform xf)
        {
            var bb = e.get_BoundingBox(null);
            if (bb == null) return new[] { 0.0, 0.0 };
            var a = xf.OfPoint(bb.Min).Z;
            var b = xf.OfPoint(bb.Max).Z;
            return new[] { Math.Min(a, b), Math.Max(a, b) };
        }

        private static double[] BboxXy(Element e, Transform xf)
        {
            var bb = e.get_BoundingBox(null);
            if (bb == null) return null;
            var corners = new[]
            {
                xf.OfPoint(bb.Min), xf.OfPoint(bb.Max),
                xf.OfPoint(new XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z)), xf.OfPoint(new XYZ(bb.Max.X, bb.Min.Y, bb.Min.Z)),
            };
            return new[] { corners.Min(c => c.X), corners.Min(c => c.Y), corners.Max(c => c.X), corners.Max(c => c.Y) };
        }

        private static List<object> Materials(Document d, CompoundStructure cs)
        {
            var mats = new List<object>();
            if (cs == null) return mats;
            foreach (var layer in cs.GetLayers())
            {
                var m = d.GetElement(layer.MaterialId) as Material;
                mats.Add(new
                {
                    name = m?.Name,
                    materialClass = m?.MaterialClass,
                    function = layer.Function.ToString(),
                    width = layer.Width,
                });
            }
            return mats;
        }

        private IEnumerable<object> CollectFloors(Source s)
        {
            foreach (var floor in new FilteredElementCollector(s.Doc).OfClass(typeof(Floor)).Cast<Floor>())
            {
                var type = s.Doc.GetElement(floor.GetTypeId()) as FloorType;
                var loops = new List<List<double[]>>();
                foreach (var reference in HostObjectUtils.GetTopFaces(floor))
                {
                    if (floor.GetGeometryObjectFromReference(reference) is Face face)
                    {
                        foreach (var loop in face.GetEdgesAsCurveLoops())
                            loops.Add(Tessellate(loop, s.Xf));
                    }
                }
                var z = ZRange(floor, s.Xf);
                yield return new
                {
                    id = Id(floor.Id),
                    source = s.Name,
                    typeName = type?.Name,
                    familyName = type?.FamilyName,
                    levelName = LevelName(s.Doc, floor.LevelId),
                    isStructural = floor.get_Parameter(BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL)?.AsInteger() == 1,
                    thickness = type?.GetCompoundStructure()?.GetWidth()
                                ?? floor.get_Parameter(BuiltInParameter.FLOOR_ATTR_THICKNESS_PARAM)?.AsDouble() ?? 0.0,
                    materials = Materials(s.Doc, type?.GetCompoundStructure()),
                    bottomElevation = z[0],
                    topElevation = z[1],
                    loops,
                };
            }
        }

        private IEnumerable<object> CollectColumns(Source s, ExportParams p)
        {
            var categories = new List<BuiltInCategory> { BuiltInCategory.OST_StructuralColumns };
            if (p.includeArchColumns) categories.Add(BuiltInCategory.OST_Columns);
            var geomOptions = new Options { DetailLevel = ViewDetailLevel.Medium, ComputeReferences = false };

            foreach (var category in categories)
            {
                var instances = new FilteredElementCollector(s.Doc).OfCategory(category)
                    .WhereElementIsNotElementType().OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>();
                foreach (var fi in instances)
                {
                    XYZ point;
                    double rotation = 0;
                    var slanted = false;
                    if (fi.Location is LocationPoint lp)
                    {
                        point = lp.Point;
                        rotation = lp.Rotation;
                    }
                    else if (fi.Location is LocationCurve lc)
                    {
                        point = lc.Curve.GetEndPoint(0);
                        slanted = true;
                    }
                    else continue;

                    var material = s.Doc.GetElement(fi.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId() ?? ElementId.InvalidElementId) as Material;
                    var z = ZRange(fi, s.Xf);
                    yield return new
                    {
                        id = Id(fi.Id),
                        source = s.Name,
                        category = category == BuiltInCategory.OST_StructuralColumns ? "structural" : "architectural",
                        familyName = fi.Symbol?.FamilyName,
                        typeName = fi.Symbol?.Name,
                        mark = fi.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)?.AsString(),
                        gridMark = fi.get_Parameter(BuiltInParameter.COLUMN_LOCATION_MARK)?.AsString(),
                        baseLevel = LevelName(s.Doc, fi.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)?.AsElementId()),
                        topLevel = LevelName(s.Doc, fi.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)?.AsElementId()),
                        baseElevation = z[0],
                        topElevation = z[1],
                        xy = Xy(s.Xf.OfPoint(point)),
                        rotation,
                        slanted,
                        material = material?.Name,
                        materialClass = material?.MaterialClass,
                        structuralMaterial = fi.StructuralMaterialType.ToString(),
                        footprint = ColumnFootprint(fi, geomOptions, s.Xf),
                    };
                }
            }
        }

        private static List<double[]> ColumnFootprint(FamilyInstance fi, Options options, Transform xf)
        {
            PlanarFace best = null;
            foreach (var solid in Solids(fi.get_Geometry(options)))
            {
                foreach (Face face in solid.Faces)
                {
                    if (face is PlanarFace pf && pf.FaceNormal.Z < -0.99 && (best == null || pf.Origin.Z < best.Origin.Z))
                        best = pf;
                }
            }
            if (best == null) return null;
            var loops = best.GetEdgesAsCurveLoops().OrderByDescending(l => l.GetExactLength()).ToList();
            return loops.Count == 0 ? null : Tessellate(loops[0], xf);
        }

        private static IEnumerable<Solid> Solids(GeometryElement geometry)
        {
            if (geometry == null) yield break;
            foreach (var obj in geometry)
            {
                if (obj is Solid solid && solid.Volume > 1e-6)
                    yield return solid;
                else if (obj is GeometryInstance gi)
                    foreach (var nested in Solids(gi.GetInstanceGeometry()))
                        yield return nested;
            }
        }

        private IEnumerable<object> CollectWalls(Source s)
        {
            foreach (var wall in new FilteredElementCollector(s.Doc).OfClass(typeof(Wall)).Cast<Wall>())
            {
                var wallType = wall.WallType;
                if (wallType == null || wallType.Kind == WallKind.Curtain) continue;
                if (!(wall.Location is LocationCurve lc)) continue;

                var structuralMaterial = s.Doc.GetElement(
                    wall.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId() ?? ElementId.InvalidElementId) as Material;
                var z = ZRange(wall, s.Xf);
                yield return new
                {
                    id = Id(wall.Id),
                    source = s.Name,
                    typeName = wallType.Name,
                    familyName = wallType.FamilyName,
                    kind = wallType.Kind.ToString(),
                    function = wallType.Function.ToString(),
                    structuralFlag = wall.get_Parameter(BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)?.AsInteger() == 1,
                    structuralUsage = wall.StructuralUsage.ToString(),
                    width = wall.Width,
                    baseLevel = LevelName(s.Doc, wall.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT)?.AsElementId()),
                    baseElevation = z[0],
                    topElevation = z[1],
                    structuralMaterial = structuralMaterial?.Name,
                    structuralMaterialClass = structuralMaterial?.MaterialClass,
                    materials = Materials(s.Doc, wallType.GetCompoundStructure()),
                    curve = Tessellate(new[] { lc.Curve }, s.Xf).Concat(new[] { Xy(s.Xf.OfPoint(lc.Curve.GetEndPoint(1))) }).ToList(),
                };
            }
        }

        private IEnumerable<object> CollectCoreMarkers(Source s)
        {
            foreach (var room in new FilteredElementCollector(s.Doc).OfCategory(BuiltInCategory.OST_Rooms)
                         .WhereElementIsNotElementType().Cast<SpatialElement>())
            {
                var name = room.get_Parameter(BuiltInParameter.ROOM_NAME)?.AsString() ?? room.Name;
                if (room.Area <= 0 || string.IsNullOrEmpty(name) || !CoreRoomName.IsMatch(name)) continue;
                var z = ZRange(room, s.Xf);
                yield return new { kind = "room", name, source = s.Name, bbox = BboxXy(room, s.Xf), baseElevation = z[0], topElevation = z[1] };
            }

            foreach (var stair in new FilteredElementCollector(s.Doc).OfCategory(BuiltInCategory.OST_Stairs).WhereElementIsNotElementType())
            {
                var z = ZRange(stair, s.Xf);
                yield return new { kind = "stair", name = stair.Name, source = s.Name, bbox = BboxXy(stair, s.Xf), baseElevation = z[0], topElevation = z[1] };
            }

            var equipment = new ElementMulticategoryFilter(new List<BuiltInCategory>
            {
                BuiltInCategory.OST_SpecialityEquipment, BuiltInCategory.OST_GenericModel,
                BuiltInCategory.OST_MechanicalEquipment, BuiltInCategory.OST_Casework,
            });
            foreach (var fi in new FilteredElementCollector(s.Doc).WherePasses(equipment).WhereElementIsNotElementType()
                         .OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>())
            {
                var label = $"{fi.Symbol?.FamilyName} {fi.Symbol?.Name}";
                if (!ElevatorFamily.IsMatch(label)) continue;
                var z = ZRange(fi, s.Xf);
                yield return new { kind = "elevator", name = label.Trim(), source = s.Name, bbox = BboxXy(fi, s.Xf), baseElevation = z[0], topElevation = z[1] };
            }
        }

        private IEnumerable<object> CollectShaftOpenings(Source s)
        {
            foreach (var opening in new FilteredElementCollector(s.Doc).OfCategory(BuiltInCategory.OST_ShaftOpening)
                         .WhereElementIsNotElementType().OfClass(typeof(Opening)).Cast<Opening>())
            {
                var curves = opening.BoundaryCurves?.Cast<Curve>().ToList() ?? new List<Curve>();
                var z = ZRange(opening, s.Xf);
                yield return new
                {
                    id = Id(opening.Id),
                    kind = "shaft",
                    source = s.Name,
                    loop = curves.Count > 0 ? Tessellate(curves, s.Xf) : null,
                    bbox = BboxXy(opening, s.Xf),
                    baseElevation = z[0],
                    topElevation = z[1],
                };
            }
        }

        private IEnumerable<object> CollectGrids(Source s)
        {
            foreach (var grid in new FilteredElementCollector(s.Doc).OfClass(typeof(Grid)).Cast<Grid>())
            {
                var curve = grid.Curve;
                if (curve == null) continue;
                yield return new
                {
                    name = grid.Name,
                    source = s.Name,
                    points = Tessellate(new[] { curve }, s.Xf).Concat(new[] { Xy(s.Xf.OfPoint(curve.GetEndPoint(1))) }).ToList(),
                };
            }
        }

        private List<object> ExportPlanViews(Document doc, List<Level> levels)
        {
            var exported = new List<object>();
            var viewType = new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                               .FirstOrDefault(t => t.ViewFamily == ViewFamily.StructuralPlan)
                           ?? new FilteredElementCollector(doc).OfClass(typeof(ViewFamilyType)).Cast<ViewFamilyType>()
                               .FirstOrDefault(t => t.ViewFamily == ViewFamily.FloorPlan);
            if (viewType == null || levels.Count == 0) return exported;

            var keep = new HashSet<BuiltInCategory>
            {
                BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_Columns, BuiltInCategory.OST_Floors,
                BuiltInCategory.OST_Walls, BuiltInCategory.OST_Grids, BuiltInCategory.OST_ShaftOpening,
                BuiltInCategory.OST_Stairs, BuiltInCategory.OST_RvtLinks,
            };
            var views = new List<ViewPlan>();
            using (var tx = new Transaction(doc, "Takedown plan views"))
            {
                tx.Start();
                foreach (var level in levels)
                {
                    try
                    {
                        var view = ViewPlan.Create(doc, viewType.Id, level.Id);
                        view.Name = "TAKEDOWN " + level.Name;
                        view.ViewTemplateId = ElementId.InvalidElementId;
                        view.DetailLevel = ViewDetailLevel.Coarse;
                        view.CropBoxActive = false;
                        foreach (Category category in doc.Settings.Categories)
                        {
                            var builtIn = category.BuiltInCategory;
                            if (keep.Contains(builtIn)) continue;
                            if (category.CategoryType != CategoryType.Model && category.CategoryType != CategoryType.Annotation) continue;
                            if (view.CanCategoryBeHidden(category.Id)) view.SetCategoryHidden(category.Id, true);
                        }
                        views.Add(view);
                    }
                    catch (Exception ex)
                    {
                        Warn($"Plan view for level '{level.Name}' not created: {ex.Message}");
                    }
                }
                tx.Commit();
            }

            var viewDir = Path.Combine(_resultDir, "views");
            Directory.CreateDirectory(viewDir);
            var options = new DXFExportOptions
            {
                FileVersion = ACADVersion.R2018,
                TargetUnit = ExportUnit.Inch,
                SharedCoords = false,
                HideScopeBox = true,
                HideReferencePlane = true,
                HideUnreferenceViewTags = true,
            };
            foreach (var view in views)
            {
                var name = Regex.Replace(view.GenLevel.Name, @"[^\w\-]+", "_");
                try
                {
                    doc.Export(viewDir, name, new List<ElementId> { view.Id }, options);
                    exported.Add(new { level = view.GenLevel.Name, file = "views/" + name + ".dxf" });
                }
                catch (Exception ex)
                {
                    Warn($"DXF export failed for '{view.GenLevel.Name}': {ex.Message}");
                }
            }
            return exported;
        }
    }
}
