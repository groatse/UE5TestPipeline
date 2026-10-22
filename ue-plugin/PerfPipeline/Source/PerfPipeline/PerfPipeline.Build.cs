using UnrealBuildTool;

public class PerfPipeline : ModuleRules
{
	public PerfPipeline(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[] { "Core" });
		PrivateDependencyModuleNames.AddRange(new string[] { "CoreUObject", "Engine", "RenderCore", "RHI", "TraceLog" });
	}
}
