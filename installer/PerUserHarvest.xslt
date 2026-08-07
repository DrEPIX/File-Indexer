<?xml version="1.0" encoding="utf-8"?>
<!--
  Heat gives harvested files file key paths. For a true per-user installation,
  Windows Installer requires every component under the user profile to use an
  HKCU registry key path (ICE38), and every created directory to have explicit
  uninstall cleanup (ICE64). Keep this deterministic transform beside the WiX
  source so those guarantees are part of the installer, not suppressed checks.
-->
<xsl:stylesheet
    version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:wix="http://schemas.microsoft.com/wix/2006/wi"
    exclude-result-prefixes="wix">
  <xsl:output method="xml" encoding="utf-8" indent="yes" />

  <xsl:template match="@*|node()">
    <xsl:copy>
      <xsl:apply-templates select="@*|node()" />
    </xsl:copy>
  </xsl:template>

  <xsl:template match="wix:File/@KeyPath">
    <xsl:attribute name="KeyPath">no</xsl:attribute>
  </xsl:template>

  <xsl:template match="wix:Component">
    <xsl:copy>
      <xsl:apply-templates select="@*|node()" />
      <wix:RegistryValue
          Root="HKCU"
          Key="Software\File Indexer\V1\Components"
          Name="{@Id}"
          Type="integer"
          Value="1"
          KeyPath="yes" />
      <xsl:if test="not(preceding-sibling::wix:Component)">
        <wix:RemoveFolder Id="Remove_{@Id}" On="uninstall" />
      </xsl:if>
    </xsl:copy>
  </xsl:template>
</xsl:stylesheet>
